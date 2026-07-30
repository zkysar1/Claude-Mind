""": ranged (offset/limit/pages) reads must reach the manifest.

THE ASYMMETRY THIS PINS. `context-reads-record.sh` computed a `partial` flag and
`exit 0`'d on it, so a ranged read was dropped on the floor. The read-before-edit
advisory then reported "has not been Read this session" for a file the agent had
demonstrably just read — on EVERY large file, because a large file is exactly the
one you read with offset/limit. A banner that is wrong most often on the files it
matters most for is worse than no banner: read-before-edit.md Rule 4 names
desensitization as the specific harm.

The drop is INVISIBLE without a two-shape control. Whole-file reads record fine,
so every single-shape test passes while the ranged path is dead. Each pair below
runs BOTH shapes against the same file for exactly that reason.

WHY A MARKER RATHER THAN A PLAIN RECORD. Recording a ranged read as an ordinary
entry would satisfy the advisory but hand `cmd_gate` — the BLOCKING PreToolUse
dedup — a reason to refuse a later WHOLE-FILE read as "Already in context." That
would collide head-on with verify-before-assuming.md's mandated re-verify, and it
would trade a noisy-but-harmless false positive for a silent loss of real context.
So partial entries carry `#partial:` and the two consumers diverge deliberately:

    cmd_check_file (advisory, non-blocking) counts full UNION partial
    read_tracker   -> cmd_gate (blocking)   counts FULL ONLY, never partial

`test_whole_file_reread_is_never_blocked_after_ranged_read` is the guard on that
split; it is the test that fails if someone later "simplifies" the marker away.

Hermetic: every test builds a throwaway agent dir under the real PROJECT_ROOT and
removes it, so the live session manifest is never touched. The real hook scripts
are invoked (canonical code path AND canonical invocation shape — the production
stdin JSON, not a hand-rolled equivalent).

Run: py -3 -m pytest core/scripts/tests/test_context_reads_partial.py -v
  or: py -3 core/scripts/tests/test_context_reads_partial.py
"""
import json
import os
import shutil
import subprocess
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent            # core/scripts
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # repo root

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402

# NARROW scope (.claude/skills): recorded, advisory-visible, AND dedup-blocking.
# The only class where the cmd_gate hazard above can actually bite.
NARROW_SCOPE_FILE = PROJECT_ROOT / ".claude" / "skills" / "respond" / "SKILL.md"
# ADVISORY-ONLY scope (core/scripts, ): recorded + advisory, never blocked.
CORE_SCRIPTS_FILE = SCRIPT_DIR / "iteration-close.sh"
# OUT of every scope: must stay untracked in both shapes.
OUT_OF_SCOPE_FILE = PROJECT_ROOT / ".claude" / "rules" / "read-before-edit.md"

PARTIAL_PREFIX = "#partial:"
THROWAWAY_AGENT = "_partial_reads_test_throwaway_agent_"
SID = "partial-reads-sid-001"


def _norm(p):
    return str(Path(p).resolve()).replace("\\", "/")


@contextmanager
def _throwaway_agent(manifest_paths=None, partial_paths=None, session_id=SID):
    """Throwaway agents/<name>/session under the real PROJECT_ROOT. Always cleaned."""
    agent_dir = PROJECT_ROOT / "agents" / THROWAWAY_AGENT
    session_dir = agent_dir / "session"
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        # newline="" disables CRLF translation on Windows (guard-1688) — both
        # files are read by the shell scripts under test.
        (agent_dir / "local-paths.conf").write_text(
            "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline=""
        )
        if manifest_paths is not None or partial_paths is not None:
            lines = [f"#session:{session_id}"]
            lines += [_norm(p) for p in (manifest_paths or [])]
            lines += [PARTIAL_PREFIX + _norm(p) for p in (partial_paths or [])]
            (session_dir / "context-reads.txt").write_text(
                "\n".join(lines) + "\n", encoding="utf-8", newline=""
            )
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY_AGENT
        yield env
    finally:
        if agent_dir.name == THROWAWAY_AGENT and agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)


def _read_json(file_path, offset=None, limit=None, pages=None, session_id=SID):
    """Production Read-hook stdin shape. offset/limit/pages => a ranged read."""
    ti = {"file_path": str(file_path)}
    if offset is not None:
        ti["offset"] = offset
    if limit is not None:
        ti["limit"] = limit
    if pages is not None:
        ti["pages"] = pages
    return json.dumps({"tool_name": "Read", "tool_input": ti,
                       "session_id": session_id})


def _edit_json(file_path, session_id=SID):
    return json.dumps({"tool_name": "Edit",
                       "tool_input": {"file_path": str(file_path),
                                      "old_string": "a", "new_string": "b"},
                       "session_id": session_id})


def _run(script_rel, stdin_text, env, timeout=60):
    r = subprocess.run([BASH, script_rel], input=stdin_text, capture_output=True,
                       text=True, env=env, timeout=timeout, cwd=str(PROJECT_ROOT))
    return r.returncode, r.stdout, r.stderr


def _record(file_path, env, **kw):
    return _run("core/scripts/context-reads-record.sh",
                _read_json(file_path, **kw), env)


def _manifest_lines(_env):
    mf = PROJECT_ROOT / "agents" / THROWAWAY_AGENT / "session" / "context-reads.txt"
    if not mf.exists():
        return []
    return [ln.strip() for ln in mf.read_text().splitlines() if ln.strip()]


def _tracked_any(env, path):
    """True if the path is in the manifest in EITHER form."""
    n = _norm(path)
    return n in _manifest_lines(env) or (PARTIAL_PREFIX + n) in _manifest_lines(env)


# ── 1. THE DROP: the two-shape control ──────────────────────────────────────

def test_whole_file_read_is_recorded():
    """Control shape. Passed before the fix too — that is the point: on its own
    it certifies a recorder that drops half its input."""
    with _throwaway_agent(manifest_paths=[]) as env:
        rc, _o, _e = _record(NARROW_SCOPE_FILE, env)
        assert rc == 0, f"record hook must exit 0, got {rc}"
        assert _norm(NARROW_SCOPE_FILE) in _manifest_lines(env), (
            f"whole-file read must be recorded. manifest={_manifest_lines(env)!r}")


def test_ranged_read_is_recorded():
    """THE BUG. Identical file, identical hook, only offset/limit differ."""
    with _throwaway_agent(manifest_paths=[]) as env:
        rc, _o, _e = _record(NARROW_SCOPE_FILE, env, offset=100, limit=50)
        assert rc == 0, f"record hook must exit 0, got {rc}"
        assert _tracked_any(env, NARROW_SCOPE_FILE), (
            "ranged read must reach the manifest — it was silently dropped, so "
            f"the edit advisory cried wolf. manifest={_manifest_lines(env)!r}")


def test_pages_read_is_recorded():
    """`pages` (PDF) is the third ranged form the recorder skipped."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(NARROW_SCOPE_FILE, env, pages="1-5")
        assert _tracked_any(env, NARROW_SCOPE_FILE), (
            f"pages read must be recorded. manifest={_manifest_lines(env)!r}")


def test_ranged_read_is_recorded_in_advisory_only_scope():
    """core/scripts is advisory-only; the ranged drop hit it too."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(CORE_SCRIPTS_FILE, env, offset=1, limit=10)
        assert _tracked_any(env, CORE_SCRIPTS_FILE), (
            f"ranged core/scripts read must record. manifest={_manifest_lines(env)!r}")


def test_out_of_scope_ranged_read_is_still_not_recorded():
    """The fix must not widen SCOPE — only stop discarding by read shape."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(OUT_OF_SCOPE_FILE, env, offset=1, limit=10)
        assert not _tracked_any(env, OUT_OF_SCOPE_FILE), (
            f"out-of-scope must stay untracked. manifest={_manifest_lines(env)!r}")


# ── 2. THE SYMPTOM: the edit advisory stops crying wolf ─────────────────────

def _advisory_fired(rc, out, err):
    return "has not been Read" in (err or "") or "has not been Read" in (out or "")


def test_edit_advisory_fires_when_never_read():
    """Control: the advisory must still work. A fix that silences it entirely
    would pass every 'no false banner' test while destroying the feature."""
    with _throwaway_agent(manifest_paths=[]) as env:
        rc, out, err = _run("core/scripts/pre-edit-context-gate.sh",
                            _edit_json(NARROW_SCOPE_FILE), env)
    assert _advisory_fired(rc, out, err), (
        f"advisory MUST fire for a never-read file. stderr={err!r}")


def test_edit_advisory_does_not_claim_unread_after_ranged_read():
    """THE USER-FACING SYMPTOM. After a ranged read the gate must not assert the
    file 'has not been Read' — that statement is simply false."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(NARROW_SCOPE_FILE, env, offset=100, limit=50)
        rc, out, err = _run("core/scripts/pre-edit-context-gate.sh",
                            _edit_json(NARROW_SCOPE_FILE), env)
    assert not _advisory_fired(rc, out, err), (
        "after a ranged read the gate must NOT claim the file was never Read "
        f"(cry-wolf). stderr={err!r}")


def test_edit_advisory_is_silent_after_whole_file_read():
    """Control shape for the pair above."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(NARROW_SCOPE_FILE, env)
        rc, out, err = _run("core/scripts/pre-edit-context-gate.sh",
                            _edit_json(NARROW_SCOPE_FILE), env)
    assert not _advisory_fired(rc, out, err), (
        f"whole-file read must silence the advisory. stderr={err!r}")


def test_partial_advisory_still_warns_about_coverage():
    """A ranged read is PARTIAL evidence, and read-before-edit.md Rule 1 says it
    counts 'only if it covers the region being edited'. The gate cannot know the
    edit region, so it must hand that judgment to the agent rather than going
    silent and implying full context. Silence here would swap a false alarm for a
    false all-clear — the strictly worse direction."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(NARROW_SCOPE_FILE, env, offset=100, limit=50)
        rc, out, err = _run("core/scripts/pre-edit-context-gate.sh",
                            _edit_json(NARROW_SCOPE_FILE), env)
        blob = (out or "") + (err or "")
    assert "in part" in blob or "ranged" in blob.lower(), (
        f"partial read must produce a COVERAGE advisory, not silence. got={blob!r}")


# ── 3. THE HAZARD: the blocking dedup gate must never see a partial ─────────

def test_whole_file_reread_is_never_blocked_after_ranged_read():
    """THE REGRESSION GUARD on the marker split.

    Naive fix: record the ranged read as an ordinary entry. cmd_gate then blocks
    the follow-up WHOLE-FILE read as 'Already in context' — refusing the read that
    would have supplied the context the ranged read lacked, and colliding with
    verify-before-assuming.md's re-verify mandate. Delete the #partial: marker and
    this test goes red."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(NARROW_SCOPE_FILE, env, offset=100, limit=50)
        rc, _o, _e = _run("core/scripts/context-reads-gate.sh",
                          _read_json(NARROW_SCOPE_FILE), env)
    assert rc == 0, (
        f"whole-file re-read after a RANGED read must be ALLOWED, got exit {rc}. "
        "A partial entry must never satisfy the blocking dedup gate.")


def test_narrow_scope_dedup_still_blocks_after_whole_file_read():
    """No regression: a genuine full-read duplicate is still blocked."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(NARROW_SCOPE_FILE, env)
        rc, _o, _e = _run("core/scripts/context-reads-gate.sh",
                          _read_json(NARROW_SCOPE_FILE), env)
    assert rc == 2, f"full-read duplicate must still be dedup-blocked, got {rc}"


def test_ranged_read_itself_is_not_blocked_by_a_prior_partial():
    """Two ranged reads of different regions are legitimate — never block them."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(NARROW_SCOPE_FILE, env, offset=1, limit=50)
        rc, _o, _e = _run("core/scripts/context-reads-gate.sh",
                          _read_json(NARROW_SCOPE_FILE, offset=500, limit=50), env)
    assert rc == 0, f"a second ranged read must be allowed, got exit {rc}"


# ── 4. Idempotency / upgrade between the two forms ──────────────────────────

def test_full_read_upgrades_a_prior_partial():
    """partial THEN full => the entry becomes FULL (so dedup starts applying)."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(NARROW_SCOPE_FILE, env, offset=1, limit=10)
        _record(NARROW_SCOPE_FILE, env)
        lines = _manifest_lines(env)
        assert _norm(NARROW_SCOPE_FILE) in lines, f"must be FULL now: {lines!r}"
        assert PARTIAL_PREFIX + _norm(NARROW_SCOPE_FILE) not in lines, (
            f"stale partial entry must be removed on upgrade: {lines!r}")
        rc, _o, _e = _run("core/scripts/context-reads-gate.sh",
                          _read_json(NARROW_SCOPE_FILE), env)
    assert rc == 2, f"after upgrade the dedup gate must block, got {rc}"


def test_partial_read_after_full_is_a_noop():
    """full THEN partial must NOT downgrade — context is not lost by re-peeking."""
    with _throwaway_agent(manifest_paths=[]) as env:
        _record(NARROW_SCOPE_FILE, env)
        _record(NARROW_SCOPE_FILE, env, offset=1, limit=10)
        lines = _manifest_lines(env)
    assert _norm(NARROW_SCOPE_FILE) in lines, f"FULL entry must survive: {lines!r}"
    assert PARTIAL_PREFIX + _norm(NARROW_SCOPE_FILE) not in lines, (
        f"must not add a partial entry alongside the full one: {lines!r}")


def test_repeated_ranged_reads_do_not_duplicate_entries():
    with _throwaway_agent(manifest_paths=[]) as env:
        for off in (1, 200, 400):
            _record(NARROW_SCOPE_FILE, env, offset=off, limit=50)
        lines = _manifest_lines(env)
    hits = [ln for ln in lines if ln.endswith(_norm(NARROW_SCOPE_FILE))]
    assert len(hits) == 1, f"expected exactly one entry, got {hits!r}"


# ── 5. Invalidation must clear BOTH forms ───────────────────────────────────

def test_invalidate_clears_a_partial_entry():
    """A tracked file edited mid-session must re-warn. If invalidate only cleared
    FULL entries, a partial entry would survive the edit and suppress the advisory
    forever — a silent false all-clear, the worst failure direction here."""
    compact = PROJECT_ROOT / "agents" / THROWAWAY_AGENT / "session" / \
        "aspirations-compact.json"
    with _throwaway_agent(manifest_paths=[]) as env:
        compact.write_text("{}")
        _record(compact, env, offset=1, limit=5)
        assert _tracked_any(env, compact), "precondition: partial entry recorded"
        subprocess.run([sys.executable, str(SCRIPT_DIR / "context-reads.py"),
                        "invalidate", str(compact)], env=env, capture_output=True,
                       text=True, timeout=60, cwd=str(PROJECT_ROOT))
        assert not _tracked_any(env, compact), (
            f"invalidate must clear the partial entry too: {_manifest_lines(env)!r}")


# ── 6. The OTHER five check-file callers must not shift ─────────────────────

def test_digest_loader_contract_unchanged_for_partial_entries():
    """load-loop-digest.sh & co. use check-file as a LOAD gate: non-empty stdout
    means 'not in context, emit the digest'. A partially-read digest is genuinely
    not fully in context, so it must still print — and must print BARE, since
    those five callers do a plain emptiness test and never parse a prefix."""
    with _throwaway_agent(manifest_paths=[], partial_paths=[NARROW_SCOPE_FILE]) as env:
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "context-reads.py"),
                            "check-file", "--session-id", SID, str(NARROW_SCOPE_FILE)],
                           env=env, capture_output=True, text=True, timeout=60,
                           cwd=str(PROJECT_ROOT))
    assert r.stdout.strip() == _norm(NARROW_SCOPE_FILE), (
        f"default check-file must print the BARE path for a partial entry "
        f"(unchanged contract for the 5 digest loaders). got={r.stdout!r}")


def test_partial_aware_check_file_distinguishes_the_two_states():
    """The advisory opts in to the richer contract; nobody else does."""
    with _throwaway_agent(manifest_paths=[], partial_paths=[NARROW_SCOPE_FILE]) as env:
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "context-reads.py"),
                            "check-file", "--partial-aware", "--session-id", SID,
                            str(NARROW_SCOPE_FILE)],
                           env=env, capture_output=True, text=True, timeout=60,
                           cwd=str(PROJECT_ROOT))
    assert r.stdout.startswith("PARTIAL"), (
        f"--partial-aware must mark a partial entry. got={r.stdout!r}")
    assert _norm(NARROW_SCOPE_FILE) in r.stdout, f"path must be present: {r.stdout!r}"


def test_partial_aware_leaves_never_read_output_bare():
    with _throwaway_agent(manifest_paths=[]) as env:
        r = subprocess.run([sys.executable, str(SCRIPT_DIR / "context-reads.py"),
                            "check-file", "--partial-aware", "--session-id", SID,
                            str(NARROW_SCOPE_FILE)],
                           env=env, capture_output=True, text=True, timeout=60,
                           cwd=str(PROJECT_ROOT))
    assert r.stdout.strip() == _norm(NARROW_SCOPE_FILE), (
        f"a never-read file must print bare even in --partial-aware. got={r.stdout!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner (this file is ALSO pytest-collected; the runner exists so it
# is not invisible to run-invisible-suites.sh)

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
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
