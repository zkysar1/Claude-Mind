""": the session provenance manifest — fetched URLs, and the query API.

WHAT THIS EXISTS FOR. An agent can write a confident claim and set a
plausible-looking URL beside it, sourced from parametric memory rather than from
anything it retrieved (the g-012-03 incident: a trade-compensation claim stated
backwards, with a citation). Prose cannot be audited for that. A manifest of what
the session ACTUALLY fetched can be, so `provenance-check.sh <url>` turns a
decorative citation into a falsifiable one.

THE STRUCTURAL HAZARD THIS PINS. Provenance entries share the context-reads
tracker file — deliberately, to inherit its session scoping, its self-healing
session-mismatch delete, and its per-Body routing. But `_read_tracker_split`'s
path fork ends in `else: full.add(line)`, which admits ANY unrecognised line into
`full` — and `full` is what `read_tracker()` returns to `cmd_gate`, the BLOCKING
PreToolUse dedup gate. A new marker prefix is kept out of that set only by being
excluded EXPLICITLY, exactly as `#partial:` is.

`test_provenance_never_inflates_the_read_tracker` is the guard on that exclusion.
It is mutation-proved: deleting the two-line skip in `_read_tracker_split` turns
it red (the count moves and the URL renders through os.path.relpath as garbage),
which a presence-only assertion would not have caught.

Hermetic: every test builds a throwaway agent dir under the real PROJECT_ROOT and
removes it, so the live session manifest is never touched. The real hook script is
invoked with the production PostToolUse stdin JSON — canonical code path AND
canonical invocation shape, not a hand-rolled equivalent.

Run: py -3 -m pytest core/scripts/tests/test_provenance_manifest.py -v
"""
import json
import os
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
from _bash_helpers import BASH                     # noqa: E402
from _context_reads_helper import norm_path as _norm  # noqa: E402

PROVENANCE_PREFIX = "#prov:"
THROWAWAY_AGENT = "_provenance_manifest_test_throwaway_agent_"
SID = "provenance-sid-001"

# A real in-narrow-scope file, so the read lane is exercised against the same
# class the blocking gate actually covers.
NARROW_SCOPE_FILE = PROJECT_ROOT / ".claude" / "skills" / "respond" / "SKILL.md"


@contextmanager
def _throwaway_agent(session_id=SID):
    agent_dir = PROJECT_ROOT / "agents" / THROWAWAY_AGENT
    session_dir = agent_dir / "session"
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "local-paths.conf").write_text(
            "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline=""
        )
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY_AGENT
        yield env
    finally:
        if agent_dir.name == THROWAWAY_AGENT and agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)


def _manifest_lines():
    mf = PROJECT_ROOT / "agents" / THROWAWAY_AGENT / "session" / "context-reads.txt"
    if not mf.exists():
        return []
    return [ln.strip() for ln in mf.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _prov_values():
    """The VALUE field of every provenance entry currently in the manifest."""
    out = []
    for ln in _manifest_lines():
        if ln.startswith(PROVENANCE_PREFIX):
            parts = ln[len(PROVENANCE_PREFIX):].split("|", 2)
            if len(parts) == 3:
                out.append(parts[2])
    return out


def _run(script_rel, stdin_text, env, timeout=60):
    r = subprocess.run([BASH, script_rel], input=stdin_text, capture_output=True,
                       text=True, env=env, timeout=timeout, cwd=str(PROJECT_ROOT))
    return r.returncode, r.stdout, r.stderr


def _fetch_hook(payload, env):
    return _run("core/scripts/context-reads-record-fetch.sh", json.dumps(payload), env)


def _read_hook(file_path, env):
    return _run("core/scripts/context-reads-record.sh",
                json.dumps({"tool_name": "Read",
                            "tool_input": {"file_path": str(file_path)},
                            "session_id": SID}), env)


def _check(query, env, session_id=SID):
    """provenance-check.sh — the query API. Returns (rc, stdout)."""
    cmd = [BASH, "core/scripts/provenance-check.sh"]
    if session_id is not None:
        cmd += ["--session-id", session_id]
    cmd.append(query)
    r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(PROJECT_ROOT))
    return r.returncode, r.stdout


def _status(env):
    r = subprocess.run([sys.executable, "core/scripts/context-reads.py", "status"],
                       capture_output=True, text=True, env=env,
                       timeout=60, cwd=str(PROJECT_ROOT))
    return r.stdout


# ── outcome[0]: fetched URLs reach the manifest, via the real hook ──────────

def test_webfetch_url_lands_in_the_manifest():
    with _throwaway_agent() as env:
        url = "https://example.test/trade-compensation-ruling"
        rc, _o, err = _fetch_hook(
            {"tool_name": "WebFetch", "session_id": SID,
             "tool_input": {"url": url, "prompt": "what does it say"}}, env)
        assert rc == 0, f"hook must exit 0, got {rc}: {err}"
        assert url in _prov_values(), (
            f"a fetched URL must reach the manifest. manifest={_manifest_lines()!r}")


def test_websearch_records_the_query_and_its_result_urls():
    """The result URLs are the half a citation would actually quote."""
    with _throwaway_agent() as env:
        hit = "https://example.test/results/one"
        rc, _o, err = _fetch_hook(
            {"tool_name": "WebSearch", "session_id": SID,
             "tool_input": {"query": "trade compensation ruling"},
             "tool_response": {"results": [{"url": hit, "title": "One"}]}}, env)
        assert rc == 0, f"hook must exit 0, got {rc}: {err}"
        values = _prov_values()
        assert "trade compensation ruling" in values, f"query missing: {values!r}"
        assert hit in values, f"result URL missing: {values!r}"


def test_fetch_hook_is_a_noop_on_an_unrelated_payload():
    """Fail-open: no url, no query => nothing written, still exit 0."""
    with _throwaway_agent() as env:
        rc, _o, _e = _fetch_hook(
            {"tool_name": "WebFetch", "session_id": SID, "tool_input": {}}, env)
        assert rc == 0
        assert _prov_values() == [], f"unexpected entries: {_manifest_lines()!r}"


# ── THE STRUCTURAL GUARD: markers must not reach the blocking dedup lane ────

def test_provenance_never_inflates_the_read_tracker():
    """Mutation-proved. Remove the PROVENANCE_PREFIX skip in _read_tracker_split
    and this goes red: the URL joins `full`, the count moves, and cmd_status
    renders it through os.path.relpath as garbage."""
    with _throwaway_agent() as env:
        _read_hook(NARROW_SCOPE_FILE, env)
        before = _status(env)
        assert "1 full" in before, f"control failed, status={before!r}"

        url = "https://example.test/should-not-be-a-tracked-file"
        _fetch_hook({"tool_name": "WebFetch", "session_id": SID,
                     "tool_input": {"url": url}}, env)

        assert url in _prov_values(), "precondition: the URL must be recorded"
        after = _status(env)
        assert "1 full" in after, (
            f"a provenance entry must NOT be counted as a tracked file read. "
            f"status={after!r}")
        assert "example.test" not in after, (
            f"a URL must never render as a tracked path. status={after!r}")

        # And it must not be reported as an unread in-scope file either.
        r = subprocess.run(
            [sys.executable, "core/scripts/context-reads.py", "check-file",
             "--session-id", SID, str(NARROW_SCOPE_FILE)],
            capture_output=True, text=True, env=env, timeout=60,
            cwd=str(PROJECT_ROOT))
        assert r.stdout.strip() == "", (
            f"the read file must still count as read. stdout={r.stdout!r}")


# ── outcome[1]: the query API answers for URLs, paths, and node keys ────────

def test_provenance_check_answers_a_fetched_url():
    with _throwaway_agent() as env:
        url = "https://example.test/cited-page"
        _fetch_hook({"tool_name": "WebFetch", "session_id": SID,
                     "tool_input": {"url": url}}, env)
        rc, out = _check(url, env)
        assert rc == 0, f"a fetched URL must answer 0, got {rc}"
        assert "RETRIEVED" in out and url in out, f"stdout={out!r}"
        assert "url" in out, f"must report HOW it was retrieved. stdout={out!r}"


def test_provenance_check_rejects_a_url_never_fetched():
    """The half that makes it worth running: an uncited-but-plausible URL."""
    with _throwaway_agent() as env:
        _fetch_hook({"tool_name": "WebFetch", "session_id": SID,
                     "tool_input": {"url": "https://example.test/real"}}, env)
        rc, out = _check("https://example.test/never-fetched", env)
        assert rc == 1, f"an unfetched URL must answer 1, got {rc}: {out!r}"


def test_provenance_check_answers_file_paths_from_the_read_lane():
    """One query surface: a Read and a WebFetch are both provenance."""
    with _throwaway_agent() as env:
        rc, out = _check(str(NARROW_SCOPE_FILE), env)
        assert rc == 1, f"unread file must answer 1, got {rc}: {out!r}"
        _read_hook(NARROW_SCOPE_FILE, env)
        rc, out = _check(str(NARROW_SCOPE_FILE), env)
        assert rc == 0, f"a file read this session must answer 0, got {rc}"
        assert _norm(NARROW_SCOPE_FILE) in out, f"stdout={out!r}"


def test_provenance_check_answers_a_tree_node_key():
    with _throwaway_agent() as env:
        node = "system/system-constraints-loop/external-path-resolution-cruft"
        rc, _o = _check(node, env)
        assert rc == 1, "a node not retrieved must answer 1"
        subprocess.run(
            [sys.executable, "core/scripts/context-reads.py", "record-prov",
             "--session-id", SID, "--kind", "node", node],
            capture_output=True, text=True, env=env, timeout=60,
            cwd=str(PROJECT_ROOT))
        rc, out = _check(node, env)
        assert rc == 0, f"a recorded node must answer 0, got {rc}"
        assert "node" in out and node in out, f"stdout={out!r}"


def test_a_stale_session_manifest_answers_no():
    """Compaction semantics: the manifest is session-scoped. A different SID must
    never inherit the previous session's provenance as its own evidence."""
    with _throwaway_agent() as env:
        url = "https://example.test/previous-session"
        _fetch_hook({"tool_name": "WebFetch", "session_id": SID,
                     "tool_input": {"url": url}}, env)
        assert _check(url, env)[0] == 0, "precondition: recorded under SID"
        rc, out = _check(url, env, session_id="a-different-session")
        assert rc == 1, (
            f"another session's fetch must not answer as this session's "
            f"evidence, got {rc}: {out!r}")


# ── the WIRING, pinned structurally (guard-1943) ───────────────────────────

def test_tree_read_records_node_provenance_only_on_success():
    """A working recorder says nothing about whether anything CALLS it.

    Read as source rather than by driving tree-read.sh, because the daemon is an
    environment dependency and the property under test is about which BRANCH the
    call sits on — an absence no successful invocation can witness. Success
    gating is the load-bearing half: recording before the response would
    authenticate a citation to a node the lookup never resolved, the one
    direction this manifest must never fail in (measured live: a nonexistent key
    exits 1 and records nothing).
    """
    src = (SCRIPT_DIR / "tree-read.sh").read_text(encoding="utf-8")
    assert "_record_node_provenance()" in src, "the recorder helper is gone"
    assert "record-prov" in src, "tree-read.sh no longer records node provenance"

    # Guarded to an explicit single-node read — computational flags are not
    # citations, and must not pay the cost.
    helper = src[src.index("_record_node_provenance()"):]
    helper = helper[:helper.index("\n}")]
    assert '[ -n "$NODE" ]' in helper, "the recorder must be scoped to --node"
    assert "|| true" in helper, "the recorder must be fail-open"

    # Both success exits call it; the failure exits must NOT.
    assert "0) _record_node_provenance; exit 0;;" in src, (
        "the primary rc=0 branch must record")
    assert 'if [ "$rc" = "0" ]; then _record_node_provenance; exit 0; fi' in src, (
        "the autospawn-retry rc=0 branch must record")
    assert "2) exit 1;;" in src, (
        "the not-found branch must stay bare — recording a failed lookup would "
        "authenticate a citation that never resolved")


def test_provenance_kinds_are_a_closed_set():
    """record-prov rejects an unknown kind rather than writing a lane nothing
    queries."""
    with _throwaway_agent() as env:
        r = subprocess.run(
            [sys.executable, "core/scripts/context-reads.py", "record-prov",
             "--session-id", SID, "--kind", "not-a-real-kind", "x"],
            capture_output=True, text=True, env=env, timeout=60,
            cwd=str(PROJECT_ROOT))
        assert r.returncode != 0, "an unknown kind must be refused"
        assert _prov_values() == [], f"nothing may be written: {_manifest_lines()!r}"


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'FAILED' if failed else 'ALL PASSED'} ({failed} failures)")
    sys.exit(1 if failed else 0)
