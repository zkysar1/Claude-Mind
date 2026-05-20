""": stdin reconfigure helper test.

Verifies core/scripts/_io.py reconfigure_stdio() unit semantics:
  - Function is callable
  - Real text streams get switched to utf-8
  - Idempotent (callable twice, no error)
  - Survives streams without .reconfigure (test harness StringIO)
  - Survives missing streams (sys.stdin = None edge case)

Plus an integration smoke test: invokes a Tier A script via subprocess
with cp1252-shaped input and confirms the script doesn't crash AND the
input round-trips through the JSONL writer cleanly.

Run: py -3 core/scripts/tests/test_io_reconfigure_stdio.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

# Make _io importable
SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import _stdio as ayoai_io  # local module — name avoids stdlib `_io` collision


def assert_no_raise(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception as e:
        raise AssertionError(f"Expected no exception, got {type(e).__name__}: {e}")


def test_helper_is_callable():
    assert callable(ayoai_io.reconfigure_stdio), "reconfigure_stdio must be callable"


def test_helper_runs_clean_on_real_streams():
    """When stdin/stdout/stderr are the real CPython streams,
    reconfigure_stdio() must not raise."""
    assert_no_raise(ayoai_io.reconfigure_stdio)


def test_helper_is_idempotent():
    """Two calls in a row must produce the same final encoding state."""
    ayoai_io.reconfigure_stdio()
    enc_after_first = sys.stdout.encoding
    ayoai_io.reconfigure_stdio()
    enc_after_second = sys.stdout.encoding
    assert enc_after_first.lower() == "utf-8", f"Expected utf-8, got {enc_after_first!r}"
    assert enc_after_second.lower() == "utf-8", f"Expected utf-8, got {enc_after_second!r}"


def test_helper_survives_stringio_streams():
    """Test harnesses (and pytest capturing) replace sys.stdin/stdout with
    io.StringIO objects that don't have .reconfigure(). The helper must
    silently skip them."""
    saved_stdin = sys.stdin
    saved_stdout = sys.stdout
    try:
        sys.stdin = io.StringIO("buffer")
        sys.stdout = io.StringIO()
        # Must not raise even though StringIO has no .reconfigure
        assert_no_raise(ayoai_io.reconfigure_stdio)
    finally:
        sys.stdin = saved_stdin
        sys.stdout = saved_stdout


def test_helper_survives_none_streams():
    """If sys.stdin is None (some embedded cases), helper must handle it."""
    saved_stdin = sys.stdin
    try:
        sys.stdin = None
        assert_no_raise(ayoai_io.reconfigure_stdio)
    finally:
        sys.stdin = saved_stdin


def test_subprocess_pipe_with_em_dash_round_trips():
    """Integration smoke test: pipe a payload containing an em-dash
    through `py -3 -c` that imports _io, reconfigures stdio, reads
    stdin, and prints JSON. The round-tripped string must match the
    input even when no PYTHONIOENCODING env is set."""
    em_dash_text = "before — after"
    payload = json.dumps({"text": em_dash_text})

    code = (
        "import sys, json\n"
        f"sys.path.insert(0, r'{SCRIPT_DIR}')\n"
        "from _stdio import reconfigure_stdio\n"
        "reconfigure_stdio()\n"
        "data = json.loads(sys.stdin.read())\n"
        "print(json.dumps(data, ensure_ascii=False))\n"
    )

    # Strip PYTHONIOENCODING from env to prove _io.py works on its own
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)

    result = subprocess.run(
        [sys.executable, "-c", code],
        input=payload.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"Subprocess crashed: rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    out = result.stdout.decode("utf-8").strip()
    parsed = json.loads(out)
    assert parsed["text"] == em_dash_text, (
        f"Em-dash mangled: in={em_dash_text!r} out={parsed['text']!r}"
    )


def test_aspirations_py_imports_io_helper():
    """Static check: the 5 Tier A scripts MUST import reconfigure_stdio
    from _io (regression guard against someone reverting the change)."""
    # journal.py removed in H2 Wave 1 (CLI migrated to the generic daemon
    # store endpoint; zero importers). Its stdio-reconfigure guard is moot —
    # daemon-side I/O is handled by the server, not a per-call CLI process.
    tier_a = [
        "aspirations.py",
        "experience.py",
        "pipeline.py",
        "board.py",
    ]
    for name in tier_a:
        path = SCRIPT_DIR / name
        body = path.read_text(encoding="utf-8")
        assert "from _stdio import reconfigure_stdio" in body, (
            f"{name} missing reconfigure_stdio import (g-276-04)"
        )
        assert "reconfigure_stdio()" in body, (
            f"{name} imports but does not call reconfigure_stdio() (g-276-04)"
        )


def test_tier_b_writers_import_io_helper():
    """Static check: 14 Tier B writers MUST import reconfigure_stdio
    from _stdio (regression guard, g-276-05)."""
    tier_b = [
        # reasoning-bank.py removed in H2 Wave 2 (2026-05-15): CLI gutted,
        # module retained as library only — no longer a Tier B writer.
        "pattern-signatures.py",
        "spark-questions.py",
        "decision-rules-append.py",
        "execution-diary.py",
        "conclusion-record.py",
        "loop-state-save.py",
        "wm.py",
        "tree.py",
        "meta-yaml.py",
        "mind-yaml.py",
        "skill-relations.py",
        "reflect-bookkeeping.py",
        "reasoning-snapshot.py",
        "handoff-yaml-build.py",
    ]
    for name in tier_b:
        path = SCRIPT_DIR / name
        body = path.read_text(encoding="utf-8")
        assert "from _stdio import reconfigure_stdio" in body, (
            f"{name} missing reconfigure_stdio import (g-276-05 Tier B)"
        )
        assert "reconfigure_stdio()" in body, (
            f"{name} imports but does not call reconfigure_stdio() (g-276-05 Tier B)"
        )


def test_tier_c_gates_import_io_helper():
    """Static check: 6 Tier C gates MUST import reconfigure_stdio
    from _stdio (regression guard, g-276-05)."""
    tier_c = [
        "blocker-create-gate.py",
        "goal-duplication-gate.py",
        "origin-signal-gate.py",
        "loop-state-merge-gate.py",
        "target-state-probe.py",
        "meta-dead-ends.py",
    ]
    for name in tier_c:
        path = SCRIPT_DIR / name
        body = path.read_text(encoding="utf-8")
        assert "from _stdio import reconfigure_stdio" in body, (
            f"{name} missing reconfigure_stdio import (g-276-05 Tier C)"
        )
        assert "reconfigure_stdio()" in body, (
            f"{name} imports but does not call reconfigure_stdio() (g-276-05 Tier C)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test runner

if __name__ == "__main__":
    tests = [
        ("helper_is_callable", test_helper_is_callable),
        ("helper_runs_clean_on_real_streams", test_helper_runs_clean_on_real_streams),
        ("helper_is_idempotent", test_helper_is_idempotent),
        ("helper_survives_stringio_streams", test_helper_survives_stringio_streams),
        ("helper_survives_none_streams", test_helper_survives_none_streams),
        ("subprocess_pipe_with_em_dash_round_trips", test_subprocess_pipe_with_em_dash_round_trips),
        ("aspirations_py_imports_io_helper", test_aspirations_py_imports_io_helper),
        ("tier_b_writers_import_io_helper", test_tier_b_writers_import_io_helper),
        ("tier_c_gates_import_io_helper", test_tier_c_gates_import_io_helper),
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            failures += 1
            print(f"  FAIL  {name}: {e}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")
