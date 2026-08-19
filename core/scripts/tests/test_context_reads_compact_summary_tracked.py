""": the compact SUMMARY path must be tracked by context-reads.

WHY THIS FILE EXISTS. `load-aspirations-compact.sh` ends with

    python3 context-reads.py check-file "$COMPACT_SUMMARY"

and every caller consumes it as `IF path returned: Read it`. The summary lives at
agents/<agent>/session/aspirations-compact-summary.json, which matches no entry
in TRACKED_PREFIXES, so it is in scope ONLY if it is named in TRACKED_FILES. It
was not. `cmd_check_file` therefore hit its out-of-scope `continue` and printed
NOTHING on rc=0 — unconditionally, every invocation, every agent, every box —
and the caller's IF never fired. The loop then ran with no portfolio in context:
precheck could not compute active_count, strategic-scan S1 reviewed zero
recurring goals, S3/S4a computed over an empty list, and each reported a clean
pass. Five-plus sightings by two agents on two boxes over ten days, never
tracked as work.

THE PIN THAT MATTERS IS NON-EMPTY STDOUT. The behaviour was rc=0 the whole time,
so a test asserting only the exit code passes against the bug — which is exactly
how it survived those five sightings. test_check_file_prints_summary_when_unread
is the mutation-proof case: revert the TRACKED_FILES entry and it fails.

The dedup and invalidate cases are here so the fix cannot degrade into the
opposite bug (always print). All three branches are pinned: unread -> prints,
recorded -> silent, invalidated -> prints again.

Everything runs context-reads.py as a SUBPROCESS with MIND_AGENT pointing at a
throwaway agent dir. TRACKED_FILES is a module-level constant derived from
AGENT_DIR at import time, so an in-process import would bake in whatever agent
the test runner happens to be bound to (guard-577).

Run: py -3 -m pytest core/scripts/tests/test_context_reads_compact_summary_tracked.py -v
  or: py -3 core/scripts/tests/test_context_reads_compact_summary_tracked.py
"""
import os
import re
import shutil
import subprocess
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent            # core/scripts
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # repo root

CONTEXT_READS = SCRIPT_DIR / "context-reads.py"
LOADER = SCRIPT_DIR / "load-aspirations-compact.sh"

THROWAWAY_AGENT = "_compact_summary_test_throwaway_agent_"
SID = "compact-summary-sid-001"

SUMMARY_NAME = "aspirations-compact-summary.json"
FULL_NAME = "aspirations-compact.json"


def _norm(p):
    return str(Path(p).resolve()).replace("\\", "/")


@contextmanager
def _throwaway_agent(manifest_paths=None, session_id=SID):
    """Create agents/<throwaway>/session under the real PROJECT_ROOT, seeding both
    compact files plus an optional context-reads.txt manifest. Always cleans up."""
    agent_dir = PROJECT_ROOT / "agents" / THROWAWAY_AGENT
    session_dir = agent_dir / "session"
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        # newline="" disables CRLF translation on Windows (guard-1688).
        (agent_dir / "local-paths.conf").write_text(
            "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline=""
        )
        # Both compact files must EXIST — the defect is about a file that is
        # present and fresh yet still yields empty stdout.
        (session_dir / FULL_NAME).write_text("[]", encoding="utf-8", newline="")
        (session_dir / SUMMARY_NAME).write_text("[]", encoding="utf-8", newline="")
        if manifest_paths is not None:
            lines = [f"#session:{session_id}"] + [_norm(p) for p in manifest_paths]
            (session_dir / "context-reads.txt").write_text(
                "\n".join(lines) + "\n", encoding="utf-8", newline=""
            )
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY_AGENT
        yield env, session_dir
    finally:
        if agent_dir.name == THROWAWAY_AGENT and agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)


def _cr(env, *args, timeout=30):
    """Invoke context-reads.py in a subprocess so TRACKED_FILES is recomputed
    from this test's MIND_AGENT rather than the runner's (guard-577)."""
    r = subprocess.run(
        [sys.executable, str(CONTEXT_READS), *args],
        capture_output=True, text=True, env=env,
        timeout=timeout, cwd=str(PROJECT_ROOT),
    )
    return r.returncode, r.stdout, r.stderr


def _manifest_lines(session_dir):
    mf = session_dir / "context-reads.txt"
    if not mf.exists():
        return []
    return [l.strip() for l in mf.read_text().splitlines() if l.strip()]


# ── THE PIN: non-empty stdout, not merely rc==0 ──────────────────────────────

def test_check_file_prints_summary_when_unread():
    """The mutation-proof case. Remove the TRACKED_FILES entry and this fails;
    a test asserting only rc==0 would keep passing, which is the whole reason
    this defect survived five sightings."""
    with _throwaway_agent(manifest_paths=[]) as (env, session_dir):
        target = session_dir / SUMMARY_NAME
        rc, out, err = _cr(env, "check-file", str(target))
    assert rc == 0, f"check-file must exit 0, got {rc}. stderr={err!r}"
    assert out.strip(), (
        "check-file printed NOTHING for an existing, unread compact summary. "
        "Callers use `IF path returned: Read it`, so empty stdout silently "
        "drops the entire portfolio (g-115-4861). Check that "
        f"{SUMMARY_NAME!r} is named in context-reads.py TRACKED_FILES."
    )
    assert _norm(target) == out.strip(), (
        f"check-file must print the normalized summary path, got {out.strip()!r}")


def test_check_file_silent_when_summary_already_read():
    """The other branch: once recorded, the path must NOT be re-emitted. Guards
    against the fix degrading into an always-print bug, which would make every
    caller re-Read a 600KB file on every loop phase."""
    with _throwaway_agent() as (env, session_dir):
        target = session_dir / SUMMARY_NAME
        _cr(env, "record", str(target))
        rc, out, _err = _cr(env, "check-file", str(target))
    assert rc == 0, f"check-file must exit 0, got {rc}"
    assert not out.strip(), (
        f"an already-read summary must NOT be re-emitted, got {out.strip()!r}")


def test_invalidate_untracks_summary():
    """load-aspirations-compact.sh invalidates the summary after regenerating it.
    That call was a silent no-op for as long as the summary was untracked — which
    is what proves the omission was an oversight, not a design choice."""
    with _throwaway_agent() as (env, session_dir):
        target = session_dir / SUMMARY_NAME
        _cr(env, "record", str(target))
        assert _norm(target) in _manifest_lines(session_dir), "precondition: recorded"
        rc, _out, _err = _cr(env, "invalidate", str(target))
        assert rc == 0, f"invalidate must exit 0, got {rc}"
        assert _norm(target) not in _manifest_lines(session_dir), (
            "invalidate must untrack the summary so a regenerated compact is "
            "re-emitted to callers")
        rc, out, _err = _cr(env, "check-file", str(target))
    assert out.strip(), "after invalidate, check-file must print the path again"


def test_full_compact_still_tracked():
    """Positive control + non-regression: the full compact was always tracked and
    must stay so. If BOTH files came back empty the failure would be in the
    harness, not in TRACKED_FILES — this case tells those apart."""
    with _throwaway_agent(manifest_paths=[]) as (env, session_dir):
        target = session_dir / FULL_NAME
        rc, out, _err = _cr(env, "check-file", str(target))
    assert rc == 0 and out.strip() == _norm(target), (
        f"full compact must still be tracked; rc={rc} out={out.strip()!r}")


# ── Anti-drift: the emitted filename and the tracked filename must agree ─────

def test_loader_emits_a_filename_that_tracked_files_names():
    """The defect class, pinned structurally. The summary projection was added to
    the loader and the constant was never updated to match, so the two drifted
    apart silently. Any future rename of either side re-breaks the loader; this
    case makes that a red test instead of five more sightings."""
    loader_src = LOADER.read_text(encoding="utf-8", errors="replace")
    engine_src = CONTEXT_READS.read_text(encoding="utf-8", errors="replace")

    m = re.search(r'^COMPACT_SUMMARY=.*?/([A-Za-z0-9._-]+\.json)"?\s*$',
                  loader_src, re.M)
    assert m, ("could not find the COMPACT_SUMMARY assignment in "
               f"{LOADER.name} — if it was renamed, update this test")
    emitted = m.group(1)

    assert f'check-file" "$COMPACT_SUMMARY"' in loader_src.replace("'", '"') \
        or "$COMPACT_SUMMARY" in loader_src, (
        "loader no longer emits COMPACT_SUMMARY — re-derive what it does emit")

    tracked_block = re.search(r"TRACKED_FILES\s*=\s*\[(.*?)\]", engine_src, re.S)
    assert tracked_block, "could not locate TRACKED_FILES in context-reads.py"
    assert emitted in tracked_block.group(1), (
        f"load-aspirations-compact.sh emits {emitted!r} but context-reads.py "
        f"TRACKED_FILES does not name it. That path is under "
        f"agents/<agent>/session/, which matches no TRACKED_PREFIXES, so it is "
        f"out of scope and check-file returns EMPTY on rc=0 forever (g-115-4861)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner (outside pytest)

if __name__ == "__main__":
    tests = [
        ("check_file_prints_summary_when_unread",
         test_check_file_prints_summary_when_unread),
        ("check_file_silent_when_summary_already_read",
         test_check_file_silent_when_summary_already_read),
        ("invalidate_untracks_summary", test_invalidate_untracks_summary),
        ("full_compact_still_tracked", test_full_compact_still_tracked),
        ("loader_emits_a_filename_that_tracked_files_names",
         test_loader_emits_a_filename_that_tracked_files_names),
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
