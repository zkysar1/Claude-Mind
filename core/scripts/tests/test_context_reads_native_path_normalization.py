""": context-reads normalize_path must handle NATIVE (backslash) paths.

Claude Code sends `file_path` in NATIVE form, so on Windows every path arrives
backslashed. `normalize_path` resolved FIRST and replaced separators AFTER,
which is too late: on a POSIX Path implementation a backslashed string contains
no separators at all, so it parses as ONE relative filename component and
resolve() glues CWD onto the front. The result is <cwd>/<whole-original-path>,
which matches no tracked prefix — so the read was dropped silently at rc=0.

Downstream that inverts the read-before-edit advisory from never-firing to
ALWAYS-firing: its manifest is empty, so it reports "has not been Read this
session" about files just read in full. read-before-edit.md Rule 4 states
plainly that a banner firing on correctly-read files is worse than a silent
one, because it trains the reader to dismiss it.

WHY THESE TESTS USE THE BACKSLASH SHAPE (guard-920). The four pre-existing
context-reads test helpers each re-implement the normalizer as
`str(Path(p).resolve()).replace("\\\\", "/")` — the exact buggy ordering — and
they pass today precisely because they only ever feed it forward-slash paths.
A test written in the hand-test shape cannot catch this defect; only the
production shape can. Note the defect is NOT platform-gated even though its
production IMPACT is: feeding a backslashed path to the normalizer reproduces
it on Linux too (verified while fixing), because the bug is in input handling,
not in a platform mechanism.

Invariants:
  A. Both separator forms of an in-scope path normalize to the SAME string.
  B. A normalized native path is absolute and un-doubled (the specific
     <cwd>/<whole-path> corruption never reappears).
  C. Both forms agree on advisory scope for in-scope paths (they record).
  D. Both forms agree for OUT-of-scope paths (they still do NOT record) —
     pins that the fix normalized separators without widening scope.
  E. End-to-end: context-reads-record.sh fed a PRODUCTION hook payload whose
     file_path is natively backslashed actually lands in the manifest.

Run: py -3 -m pytest core/scripts/tests/test_context_reads_native_path_normalization.py -v
"""
import importlib.util
import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent            # core/scripts
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # repo root

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402


def _load_context_reads():
    """Import context-reads.py (hyphenated — not importable by name).

    MUST cancel the module-level self-destruct timer, or importing this file
    silently kills the whole pytest session. context-reads.py arms
    `threading.Timer(10, lambda: os._exit(0))` at import — a watchdog for the
    short-lived PostToolUse hook subprocess, where a killed bash parent can
    strand the Python child (Windows does not propagate SIGTERM). That is
    correct for a process that lives milliseconds. Imported into a LONG-RUNNING
    pytest process it fires ~10s after collection and calls `os._exit(0)`,
    which terminates the interpreter immediately: no traceback, no pytest
    epilogue, no summary line — and exit status **0**, i.e. "success".

    The failure that produces is uniquely deceptive, so it is worth naming
    (measured g-240-105, 2026-07-31): the death point is determined by the
    CLOCK, not by any test, so it lands on a different test each run and reads
    as flaky/contended. `run-full-suite.py` reported `VERDICT: INVALID
    (contended)` with `chunk 04 stopped at 13%` across two runs — and the chunk
    reproduced it running SOLO, exit 0, an 84-byte log with zero NUL bytes.
    Small batches pass (they finish inside 10s), which is why this file's own
    4 tests are green alone and green paired. Only a batch running >10s dies.
    """
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location(
        "_cr_under_test", str(SCRIPT_DIR / "context-reads.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    timer = getattr(mod, "_timer", None)
    assert timer is not None, (
        "context-reads.py no longer exposes `_timer`. If the self-destruct "
        "watchdog was renamed or moved, cancel the new one HERE — otherwise "
        "this import re-arms a 10s os._exit(0) inside pytest and the suite "
        "dies mid-run with exit status 0 (see the docstring above)."
    )
    timer.cancel()
    return mod


CR = _load_context_reads()

# In-scope under the WIDE (advisory/recorder) predicate.
IN_SCOPE = [
    PROJECT_ROOT / "core" / "config" / "conventions" / "temp-store.md",
    PROJECT_ROOT / "core" / "scripts" / "context-reads.py",
    PROJECT_ROOT / ".claude" / "skills" / "respond" / "SKILL.md",
]
# Deliberately OUT of scope — a read of these is never recorded, by design
# (read-before-edit.md Rule 4: the gate is silent for these path classes).
OUT_OF_SCOPE = [
    PROJECT_ROOT / ".claude" / "rules" / "self.md",
    PROJECT_ROOT / "agents" / "foxtrot" / "self.md",
]

THROWAWAY_AGENT = "_native_path_norm_test_throwaway_agent_"
SID = "native-path-norm-sid-001"


def _native(p):
    """The production payload shape: a natively-backslashed absolute path."""
    return str(p).replace("/", "\\")


def test_both_separator_forms_normalize_identically():
    """A. Separator form must not change the normalized result."""
    for p in IN_SCOPE:
        fwd = CR.normalize_path(str(p).replace("\\", "/"))
        native = CR.normalize_path(_native(p))
        assert fwd == native, (
            f"separator form changed the normalized path for {p}:\n"
            f"  forward : {fwd}\n  native  : {native}"
        )


def test_native_path_is_not_doubled():
    """B. Pin the specific corruption: <cwd>/<whole-original-path>."""
    for p in IN_SCOPE:
        norm = CR.normalize_path(_native(p))
        assert Path(norm).is_absolute(), f"not absolute: {norm}"
        # The doubling signature is the project root appearing twice.
        root = str(PROJECT_ROOT).replace("\\", "/")
        assert norm.count(root) == 1, (
            f"path doubled (resolve-before-replace regression): {norm}"
        )
        assert "\\" not in norm, f"backslash survived normalization: {norm}"


def test_advisory_scope_agrees_across_separator_forms():
    """C+D. Both forms agree, and the fix did not widen scope."""
    for p in IN_SCOPE:
        fwd = CR.is_in_scope_advisory(CR.normalize_path(str(p).replace("\\", "/")))
        native = CR.is_in_scope_advisory(CR.normalize_path(_native(p)))
        assert fwd is True, f"expected in-scope, got False (forward): {p}"
        assert native is True, f"NATIVE form dropped from advisory scope: {p}"
    for p in OUT_OF_SCOPE:
        fwd = CR.is_in_scope_advisory(CR.normalize_path(str(p).replace("\\", "/")))
        native = CR.is_in_scope_advisory(CR.normalize_path(_native(p)))
        assert fwd is False, f"scope widened (forward form): {p}"
        assert native is False, f"scope widened (native form): {p}"


@contextmanager
def _throwaway_agent():
    """Real agents/<throwaway>/session dir so the recorder has somewhere to write."""
    agent_dir = PROJECT_ROOT / "agents" / THROWAWAY_AGENT
    session_dir = agent_dir / "session"
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        yield session_dir
    finally:
        shutil.rmtree(agent_dir, ignore_errors=True)


def test_recorder_records_a_native_payload_end_to_end():
    """E. The PRODUCTION shape: hook JSON carrying a backslashed file_path."""
    target = IN_SCOPE[0]
    with _throwaway_agent() as session_dir:
        payload = json.dumps(
            {
                "tool_input": {"file_path": _native(target)},
                "session_id": SID,
            }
        )
        proc = subprocess.run(
            [BASH, str(SCRIPT_DIR / "context-reads-record.sh")],
            input=payload,
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env={**__import__("os").environ, "MIND_AGENT": THROWAWAY_AGENT},
        )
        assert proc.returncode == 0, (
            f"recorder exited {proc.returncode}\nstderr: {proc.stderr}"
        )
        manifest = session_dir / "context-reads.txt"
        assert manifest.exists(), (
            "recorder wrote no manifest for a native-backslash payload "
            f"(stdout={proc.stdout!r} stderr={proc.stderr!r})"
        )
        body = manifest.read_text(encoding="utf-8")
        expected = CR.normalize_path(str(target))
        assert expected in body, (
            "native-backslash read was DROPPED (g-240-105 regression).\n"
            f"  expected entry: {expected}\n  manifest:\n{body}"
        )


def main():
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures.append((name, exc))
                print(f"FAIL {name}: {exc}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
