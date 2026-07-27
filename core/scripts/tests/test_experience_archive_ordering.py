"""Regression tests for : experience archive-goal must not consume
the caller's trace file on a post-copy validation failure.

The old ordering (os.replace BEFORE _validate_record) made every failed
attempt destructive: the trace was moved to the canonical content_path, the
validator then rejected the record, and the retry hit trace_missing +
content_path_exists (observed 5-attempt sequence during the g-115-1943
close). The fix copies (not moves), removes the copy on every post-copy
failure path, and deletes the source only after the record lands.

Covers the daemon endpoint (the live path) via DaemonFixture and the CLI
mirror (experience.py cmd_archive_goal) via direct invocation — the
guard-742 half-fix class requires both sides in sync.
"""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ROOT = Path(__file__).resolve().parents[3]

BAD_ANCHORS = {"verbatim_anchors": ["a-bare-string-not-a-dict"]}
GOOD_ANCHORS = {"verbatim_anchors": [{"key": "k1", "content": "c1"}]}


def _post(port, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/experience/archive-goal",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"X-Mind-Agent": "alpha", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _seed_trace(tmp_path) -> Path:
    trace = tmp_path / "trace-input.md"
    trace.write_text(
        "# trace\n" + ("evidence line, long enough to clear MIN_TRACE_BYTES. " * 8),
        encoding="utf-8")
    return trace


def _body(trace, extra):
    return {
        "goal": "g-999-01", "skill_slug": "test-skill",
        "category": "framework-test",
        "summary": "a summary comfortably beyond the minimum char floor",
        "trace_file": str(trace), **extra,
    }


def test_daemon_failed_validation_preserves_trace_and_no_orphan(tmp_path):
    from _daemon_fixture import DaemonFixture

    world = tmp_path / "world"
    world.mkdir()
    with DaemonFixture(world, agent="alpha") as df:
        trace = _seed_trace(tmp_path)
        agent_dir = df.project_root / "agents" / "alpha"

        status, resp = _post(df.port, _body(trace, BAD_ANCHORS))
        assert status == 400 and resp["error"] == "validation_failed", resp
        # The defect: the old code consumed the trace here.
        assert trace.exists(), "failed attempt consumed the caller's trace file"
        orphans = list((agent_dir / "experience").glob("*.md")) \
            if (agent_dir / "experience").exists() else []
        assert orphans == [], f"failed attempt stranded orphan .md: {orphans}"


def test_daemon_retry_after_fix_succeeds_and_consumes_source(tmp_path):
    from _daemon_fixture import DaemonFixture

    world = tmp_path / "world"
    world.mkdir()
    with DaemonFixture(world, agent="alpha") as df:
        trace = _seed_trace(tmp_path)
        agent_dir = df.project_root / "agents" / "alpha"

        status, resp = _post(df.port, _body(trace, BAD_ANCHORS))
        assert status == 400, resp

        # Retry with fixed anchors — no manual restoration needed.
        status, resp = _post(df.port, _body(trace, GOOD_ANCHORS))
        assert status == 200 and resp["ok"] is True, resp
        canonical = df.project_root / resp["record"]["content_path"]
        assert canonical.exists(), "canonical .md missing after success"
        assert not trace.exists(), "success must consume the source (move semantics)"

        # Index entry landed.
        live = agent_dir / "experience.jsonl"
        recs = [json.loads(l) for l in live.read_text(encoding="utf-8").splitlines()]
        assert any(r.get("goal_id") == "g-999-01" for r in recs)


def test_daemon_preexisting_canonical_untouched_on_409(tmp_path):
    """content_path_exists fires BEFORE the copy — the pre-existing canonical
    and the trace must both survive byte-identical."""
    from _daemon_fixture import DaemonFixture

    world = tmp_path / "world"
    world.mkdir()
    with DaemonFixture(world, agent="alpha") as df:
        trace = _seed_trace(tmp_path)
        agent_dir = df.project_root / "agents" / "alpha"
        exp_dir = agent_dir / "experience"
        exp_dir.mkdir(parents=True)
        pre = exp_dir / "exp-g-999-01-test-skill.md"
        pre.write_text("PRE-EXISTING", encoding="utf-8")

        status, resp = _post(df.port, _body(trace, GOOD_ANCHORS))
        # _uniquify_id may sidestep the collision by suffixing the id (then
        # the call SUCCEEDS and pre stays untouched), or the canonical check
        # 409s. Either way: pre survives byte-identical and trace behavior is
        # consistent with the outcome.
        assert pre.read_text(encoding="utf-8") == "PRE-EXISTING"
        if status == 200:
            assert not trace.exists()  # consumed on success
        else:
            assert trace.exists()      # preserved on failure


def test_cli_failed_validation_preserves_trace_and_no_orphan(tmp_path):
    """CLI mirror (experience.py cmd_archive_goal) — same contract."""
    world = tmp_path / "world"
    agent_dir = tmp_path / "agent"
    (agent_dir).mkdir()
    world.mkdir()
    trace = _seed_trace(tmp_path)

    env = dict(os.environ)
    env.update({
        "MIND_AGENT": "testagent",
        "MIND_WORLD": str(world),
        "MIND_META": str(tmp_path / "meta"),
        "MIND_AGENT_DIR": str(agent_dir),
        "STORAGE_BACKEND": "local",
    })
    cmd = [sys.executable, str(PROJECT_ROOT / "core" / "scripts" / "experience.py"),
           "archive-goal", "--goal", "g-999-02", "--skill-slug", "test-skill",
           "--category", "framework-test",
           "--summary", "a summary comfortably beyond the minimum char floor",
           "--trace-file", str(trace)]
    r = subprocess.run(cmd, input=json.dumps(BAD_ANCHORS), capture_output=True,
                       text=True, env=env, cwd=str(PROJECT_ROOT), timeout=60)
    assert r.returncode == 1, f"expected validation failure, got {r.returncode}: {r.stdout} {r.stderr}"
    assert "Validation error" in r.stderr
    assert trace.exists(), "CLI failed attempt consumed the trace file"
    orphans = list((agent_dir / "experience").glob("*.md")) \
        if (agent_dir / "experience").exists() else []
    assert orphans == [], f"CLI failed attempt stranded orphan .md: {orphans}"

    # Retry with good anchors succeeds and consumes the source.
    r2 = subprocess.run(cmd, input=json.dumps(GOOD_ANCHORS), capture_output=True,
                        text=True, env=env, cwd=str(PROJECT_ROOT), timeout=60)
    assert r2.returncode == 0, f"retry failed: {r2.stdout} {r2.stderr}"
    assert not trace.exists(), "CLI success must consume the source"
    rec = json.loads(r2.stdout)
    assert (agent_dir / "experience" / f"{rec['id']}.md").exists()
