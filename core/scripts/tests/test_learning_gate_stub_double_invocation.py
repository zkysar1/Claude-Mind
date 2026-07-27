"""test_learning_gate_stub_double_invocation.py —  regression pin.

BUG: iteration-close.sh do_learning_gate set perf=true whenever
retrieval-session.json goal_id == GOAL_ID, without checking whether the file
was the NO-RETRIEVAL STUB the first invocation wrote. A second learning-gate
run for the same goal (operator retry, recovery re-run) read its own stub and
reported performed=true — wrongly resetting the g-115-2201 pre-apply-consult
miss streak (lenient-direction error).

FIX: the probe now emits "goal_id<TAB>stub|real" (only stubs carry
retrieval_performed:false; the daemon-written real manifest omits the field)
and the stub path fires on goal_id mismatch OR stub-kind.

These tests execute the REAL embedded python snippets extracted from
iteration-close.sh at test time (no copies to drift), compose them in the
exact double-invocation sequence, and pin the bash condition string.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "iteration-close.sh"
SRC = SCRIPT.read_text(encoding="utf-8")


def _extract_probe_py() -> str:
    """The  stub-detect probe body (between the sentinel comment's
    python3 -c opening quote and the closing quote line)."""
    m = re.search(
        r'g-115-2454 stub-detect probe.*?probe_out="\$\(python3 -c "\n(.*?)\n" 2>/dev/null',
        SRC, re.S)
    assert m, "stub-detect probe not found in iteration-close.sh"
    return m.group(1)


def _extract_stub_writer_py() -> str:
    """The no-retrieval stub writer (single-quoted python3 -c block).

    The `GID=/RET_FILE=` env prefix is this extractor's ONLY anchor, so it must
    stay unique in iteration-close.sh. When a second block reused it (g-115-3123
    added `_repair_utilization_pending` ABOVE the writer), re.search silently
    returned the wrong block and this file failed with a confusing
    FileNotFoundError. Assert uniqueness so the next collision fails loudly and
    names its own cause.
    """
    pat = r"GID=\"\$GOAL_ID\" RET_FILE=\"\$ret_file\" python3 -c '\n(.*?)\n' 2>/dev/null"
    hits = re.findall(pat, SRC, re.S)
    assert hits, "stub writer not found in iteration-close.sh"
    assert len(hits) == 1, (
        f"{len(hits)} blocks share the GID=/RET_FILE= extraction anchor — this "
        "extractor can no longer identify the stub writer. Rename the other "
        "block's env vars (see _repair_utilization_pending's RUP_ prefix)."
    )
    return hits[0]


def _run_probe(ret_file: Path) -> str:
    code = _extract_probe_py().replace("r'$ret_file'", repr(str(ret_file)))
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, check=True)
    return out.stdout.rstrip("\n")


def _write_stub(ret_file: Path, goal_id: str) -> None:
    env = dict(os.environ, GID=goal_id, RET_FILE=str(ret_file))
    subprocess.run([sys.executable, "-c", _extract_stub_writer_py()],
                   env=env, check=True)


def test_real_manifest_probes_real(tmp_path):
    """Daemon-written manifest (no retrieval_performed field) -> real."""
    f = tmp_path / "retrieval-session.json"
    f.write_text(json.dumps({"schema_version": 2, "goal_id": "g-x-1",
                             "utilization_pending": True}), encoding="utf-8")
    assert _run_probe(f) == "g-x-1\treal"


def test_explicit_true_probes_real(tmp_path):
    """Legacy manifests carrying retrieval_performed: true are real."""
    f = tmp_path / "retrieval-session.json"
    f.write_text(json.dumps({"goal_id": "g-x-2",
                             "retrieval_performed": True}), encoding="utf-8")
    assert _run_probe(f) == "g-x-2\treal"


def test_corrupt_manifest_probes_empty(tmp_path):
    """Unparseable file -> empty output -> bash takes the stub path."""
    f = tmp_path / "retrieval-session.json"
    f.write_text("{not json", encoding="utf-8")
    assert _run_probe(f) == ""


def test_double_invocation_stub_is_not_performed(tmp_path):
    """THE  sequence, on the real shipped snippets: invocation 1
    writes the stub for goal X; invocation 2's probe reads it back — it MUST
    classify as stub (goal_id matches, kind=stub), never as performed."""
    f = tmp_path / "session" / "retrieval-session.json"
    _write_stub(f, "g-x-3")                      # invocation 1: stub written
    assert json.loads(f.read_text())["retrieval_performed"] is False
    assert _run_probe(f) == "g-x-3\tstub"        # invocation 2: stub detected
    # Idempotent re-write (the fixed bash re-enters the stub path): no error,
    # still a stub.
    _write_stub(f, "g-x-3")
    assert _run_probe(f) == "g-x-3\tstub"


def test_bash_condition_requires_non_stub():
    """Pin the condition: the stub path fires on goal mismatch OR stub-kind,
    and perf=true stays inside the else of that exact condition."""
    cond = ('if [[ "$current_file_goal" != "$GOAL_ID" || '
            '"$current_file_stub" == "stub" ]]; then')
    assert cond in SRC, "g-115-2454 condition missing/reworded"
    # perf=true must not be reachable on a goal_id-only match: the old
    # goal_id-only condition must be gone.
    assert 'if [[ "$current_file_goal" != "$GOAL_ID" ]]; then' not in SRC


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
