"""Pin body-merge.sh's retrospective chain ().

The retired `exp_capture_drain.py` carried two call-site pins
(`test_drain_is_wired_into_iteration_close`, `test_call_site_passes_apply`).
The call site MOVED rather than vanished — the surviving encode lane is
`worker_retrospective.py`'s `experience` RUN_LANE, chained from
`core/scripts/body-merge.sh` because that is the only bash-owned site where
`merged_goal_ids` exists. So the pin is owed back here, plus the contract the
chaining itself introduced.

The wrapper's stdout is a PARSED CONTRACT (aspirations-consolidate reads it), so
the byte-exactness and single-writer properties are the load-bearing ones — a
second writer on that channel corrupts a consumer that never sees this file.
Tested through the REAL wrapper against stub siblings (guard-920: replicate the
production call shape), never by re-reading the source.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_bash import bash_cmd  # noqa: E402

REPO = Path(__file__).resolve().parents[3]  # tests -> scripts -> core -> repo root
WRAPPER = REPO / "core" / "scripts" / "body-merge.sh"

# Emits a TRAILING BLANK LINE deliberately. With a lone trailing newline,
# `echo "$(cat f)"` is byte-identical to `cat f` and the passthrough assertion
# below is vacuous — measured: that exact mutant passed all 7 tests. The extra
# newline is what makes `$(...)` command-substitution stripping observable, so
# the test pins the passthrough PROPERTY rather than one payload's formatting.
MERGE_STUB = """\
import json, sys
sys.stdout.write(json.dumps({{"agent": "echo", "scanned": 1, "merged_goal_ids": {ids}}}) + "\\n\\n")
sys.exit({rc})
"""

RETRO_STUB = """\
import json, sys
# Record the argv we were called with so the test can assert the call shape.
open({argv_log!r}, "w").write(json.dumps(sys.argv[1:]))
# Also read the summary we were pointed at, proving the path is usable.
idx = sys.argv.index("--from-merge-summary")
open({seen_log!r}, "w").write(open(sys.argv[idx + 1]).read())
print(json.dumps({{"encoded": 1}}))   # MUST NOT land on the wrapper's stdout
sys.exit({rc})
"""


def _stage(tmp_path, *, merge_rc=0, retro_rc=0, ids=None):
    """Copy the real wrapper next to stub siblings it will resolve via SCRIPT_DIR."""
    d = tmp_path / "scripts"
    d.mkdir()
    shutil.copy(WRAPPER, d / "body-merge.sh")
    argv_log = tmp_path / "argv.json"
    seen_log = tmp_path / "seen.json"
    (d / "body-merge.py").write_text(
        MERGE_STUB.format(ids=json.dumps(ids if ids is not None else ["g-1"]), rc=merge_rc),
        encoding="utf-8",
    )
    (d / "worker_retrospective.py").write_text(
        RETRO_STUB.format(argv_log=str(argv_log), seen_log=str(seen_log), rc=retro_rc),
        encoding="utf-8",
    )
    return d, argv_log, seen_log


def _run(script_dir):
    return subprocess.run(
        bash_cmd(str(script_dir / "body-merge.sh"), "generalize-down", "--agent", "echo"),
        capture_output=True,
        text=True,
    )


def test_retrospective_lane_is_chained_with_the_merge_summary(tmp_path):
    """The pin the retired drain's call-site test owed forward."""
    d, argv_log, seen_log = _stage(tmp_path)
    r = _run(d)
    assert r.returncode == 0, r.stderr
    argv = json.loads(argv_log.read_text())
    assert "--from-merge-summary" in argv, argv
    # It is handed a real, readable summary carrying merged_goal_ids.
    assert json.loads(seen_log.read_text())["merged_goal_ids"] == ["g-1"]


def test_stdout_is_byte_exact_passthrough_of_the_merge_summary(tmp_path):
    """`$(...)` strips trailing newlines; a re-serialization fails this.

    Mutation-proven against `echo "$(cat "$_bm_out")"` — see MERGE_STUB's note
    on why the stub's trailing blank line is load-bearing.
    """
    d, _, _ = _stage(tmp_path)
    r = _run(d)
    expected = json.dumps({"agent": "echo", "scanned": 1, "merged_goal_ids": ["g-1"]}) + "\n\n"
    assert r.stdout == expected


def test_retrospective_stdout_never_reaches_the_wrapper_stdout(tmp_path):
    """Single-writer on the parsed channel — the consumer never sees this file."""
    d, _, _ = _stage(tmp_path)
    r = _run(d)
    assert "encoded" not in r.stdout
    json.loads(r.stdout)          # still a lone, parseable document
    assert "encoded" in r.stderr  # routed, not muted (guard-2410)


def test_failing_retrospective_does_not_fail_a_successful_merge(tmp_path):
    """Fail-open contract, inherited from the drain this replaced."""
    d, _, _ = _stage(tmp_path, retro_rc=3)
    r = _run(d)
    assert r.returncode == 0
    assert "WARN" in r.stderr
    json.loads(r.stdout)


def test_merge_rc_is_preserved_and_its_summary_still_emitted(tmp_path):
    """With bare `set -e` a non-zero merge would abort before emitting (guard-614)."""
    d, argv_log, _ = _stage(tmp_path, merge_rc=2)
    r = _run(d)
    assert r.returncode == 2
    json.loads(r.stdout)
    # And the lane is skipped on a failed merge — nothing to encode.
    assert not argv_log.exists()


def test_empty_merge_summary_is_a_clean_noop(tmp_path):
    """_goal_ids_from returns [] for a missing/empty merged_goal_ids — no warning."""
    d, argv_log, _ = _stage(tmp_path, ids=[])
    r = _run(d)
    assert r.returncode == 0
    assert "WARN" not in r.stderr
    assert argv_log.exists()


def test_non_json_summary_skips_the_lane_VISIBLY(tmp_path):
    """`--output text` is an accepted flag; the skip must be loud, not silent.

    Without the shape gate the lane still "runs" and _goal_ids_from returns [] —
    a no-op indistinguishable from "nothing to encode". Found by fresh-eyes on
    this file's own first version.
    """
    d, argv_log, _ = _stage(tmp_path)
    # Re-point the merge stub at a text-shaped summary, as --output text produces.
    (d / "body-merge.py").write_text(
        'import sys\nsys.stdout.write("agent=echo scanned=1\\n")\nsys.exit(0)\n',
        encoding="utf-8",
    )
    r = _run(d)
    assert r.returncode == 0
    assert r.stdout == "agent=echo scanned=1\n"   # passthrough still byte-exact
    assert not argv_log.exists()                  # lane genuinely not invoked
    assert "NOTE" in r.stderr and "not JSON" in r.stderr


def test_retired_drain_is_gone_and_not_rewired():
    """The other half of the reconciliation: exactly ONE drain exists in the tree."""
    assert not (REPO / "core" / "scripts" / "exp_capture_drain.py").exists()
    wire = (REPO / "core" / "scripts" / "iteration-close.sh").read_text(encoding="utf-8")
    # Assert on the INVOCATION, not the bare name: the tombstone comment does not
    # currently name the file (measured), but a future edit could add it, and this
    # test must stay green for a comment while staying red for a rewire.
    assert "exp_capture_drain.py\" " not in wire
    assert "exp_capture_drain.py --apply" not in wire
    assert "_winpath \"$SCRIPT_DIR/exp_capture_drain.py\"" not in wire


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
