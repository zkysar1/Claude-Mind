"""GET /v1/utilization/{guardrails,rb}/{report,candidates} + /rules/audit.

Layers:
  1. HTTP round-trip (running_daemon): routes wired, empty zero-states,
     limit param, rules-audit structural envelope, missing-header -> 400.
  2. Byte-compat (direct handler vs the REAL CLI utilization-stats.py):
       - guardrails/rb report+candidates: FULL byte-compat with a temp WORLD
         (MIND_WORLD). Exercises ALL shared pure logic (_evidence,
         _is_candidate, _candidate_sort_key, _summarize, sort keys, the
         limit-truthiness gate).
       - rules-audit EARLY RETURN (no .claude/rules dir): daemon-side exact
         serialisation assertion — compact, default ensure_ascii=True (the CLI
         can't reach this path because its PROJECT_ROOT is the real repo, which
         HAS .claude/rules).
       - rules-audit MAIN path: quiescence-retry byte-compat against the LIVE
         repo (PROJECT_ROOT is script-location-pinned, sibling-agent dirs are
         not redirectable). A logic bug fails ALL rounds; a concurrent agent
         write fails only some -> retry isolates race from bug.

_today() is evaluated per-request in both CLI and daemon, so date-relative
fields (age_days, candidate filter) line up as long as both run on the same
calendar day (they do — same process invocation window).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UTIL_PY = REPO_ROOT / "core" / "scripts" / "utilization-stats.py"


def _iso_days_ago(n: int) -> str:
    return (datetime.now().date() - timedelta(days=n)).isoformat()


# Records crafted to exercise active/inactive, evidence>0 vs ==0, exposure
# floor, age gate, auto_flagged escape hatch, and the eligibility window.
def _guardrail_records():
    return [
        # active, zero evidence, exposed, old -> CANDIDATE
        {"id": "g-cand", "status": "active", "rule": "candidate rule body",
         "category": "x", "created": _iso_days_ago(90),
         "utilization": {"retrieval_count": 80, "times_helpful": 0,
                         "times_inferred_helpful": 0, "times_active": 0,
                         "times_cited": 0, "times_skipped": 3}},
        # active, evidence>0 -> NOT candidate, but in report
        {"id": "g-good", "status": "active", "rule": "helpful rule",
         "category": "y", "created": _iso_days_ago(60),
         "utilization": {"retrieval_count": 100, "times_helpful": 4,
                         "times_cited": 2, "times_active": 8}},
        # active, zero evidence, but auto_flagged -> CANDIDATE regardless
        {"id": "g-flag", "status": "active", "rule": "flagged rule",
         "category": "z", "created": _iso_days_ago(5),
         "auto_flagged_for_review": True,
         "utilization": {"retrieval_count": 2, "times_inferred_unknown": 5}},
        # active, zero evidence, exposed, but TOO YOUNG -> not candidate
        {"id": "g-young", "status": "active", "rule": "young rule",
         "category": "y", "created": _iso_days_ago(10),
         "utilization": {"retrieval_count": 90}},
        # retired -> excluded from report AND candidates
        {"id": "g-dead", "status": "retired", "rule": "dead rule",
         "category": "y", "created": _iso_days_ago(200),
         "utilization": {"retrieval_count": 90}},
        # active, zero evidence, exposed, old, but eligibility window in future
        {"id": "g-eligwin", "status": "active", "rule": "windowed rule",
         "category": "y", "created": _iso_days_ago(90),
         "next_review_eligible_at": _iso_days_ago(-30),
         "utilization": {"retrieval_count": 90}},
    ]


def _rb_records():
    recs = []
    for r in _guardrail_records():
        r = dict(r)
        r["title"] = r.pop("rule")
        recs.append(r)
    return recs


def _seed_world(tmp_path, name, gr=None, rb=None) -> Path:
    world = tmp_path / name
    world.mkdir(parents=True, exist_ok=True)
    if gr is not None:
        (world / "guardrails.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in gr), encoding="utf-8")
    if rb is not None:
        (world / "reasoning-bank.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rb), encoding="utf-8")
    return world


def _run_cli(world, agent_dir, args, agent="alpha", check_rc=True):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(world.parent / "meta")
    env["MIND_AGENT"] = agent
    env["MIND_AGENT_DIR"] = str(agent_dir)
    proc = subprocess.run(
        [sys.executable, str(UTIL_PY), *args],
        text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=120,
    )
    if check_rc:
        assert proc.returncode == 0, (
            f"CLI utilization-stats.py failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


class _FakePaths:
    def __init__(self, world, agent, meta=None, project_root=REPO_ROOT):
        self.world = world
        self.agent = agent
        self.meta = meta
        self.project_root = project_root

    @property
    def agents_root(self):
        # Mirrors AgentPaths.agents_root (5 glob-routing fix consumed
        # at utilization.py:283). The fake agent dir's parent plays the agents/
        # parent; sibling enumeration finds no local-paths.conf there, matching
        # the single-agent behavior these byte-compat tests pin.
        return self.agent.parent if self.agent is not None else REPO_ROOT / "agents"


class _FakeCtx:
    def __init__(self, world=None, agent=None, meta=None, project_root=REPO_ROOT,
                 query=None, headers=None):
        self.paths = _FakePaths(world, agent, meta, project_root)
        self.query = query or {}
        self.headers = headers if headers is not None else {"x-mind-agent": "alpha"}
        self.body = None


def _http(port, path, query=None, agent="alpha"):
    qs = ("?" + urllib.parse.urlencode(query)) if query else ""
    url = f"http://127.0.0.1:{port}{path}{qs}"
    req = urllib.request.Request(url, method="GET")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

def test_guardrails_report_empty(running_daemon):
    _, port = running_daemon
    status, body = _http(port, "/v1/utilization/guardrails/report")
    assert status == 200
    out = json.loads(body)
    assert out["kind"] == "guardrails"
    assert "active_count" in out and isinstance(out["items"], list)


def test_rb_candidates_limit(running_daemon):
    _, port = running_daemon
    status, body = _http(port, "/v1/utilization/rb/candidates", {"limit": "5"})
    assert status == 200
    out = json.loads(body)
    assert out["kind"] == "rb"
    assert out["limit"] == 5


def test_candidates_invalid_limit_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "/v1/utilization/guardrails/candidates", {"limit": "abc"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("expected 400 for non-int limit")


def test_rules_audit_over_http(running_daemon):
    # The conftest temp repo has no .claude/rules -> the route exercises the
    # no_rules_dir EARLY RETURN over the full HTTP path: compact serialisation
    # (no indent, default ensure_ascii=True) + trailing newline.
    project_root, port = running_daemon
    status, body = _http(port, "/v1/utilization/rules/audit")
    assert status == 200
    rules_dir = project_root / ".claude" / "rules"
    expected = json.dumps({"status": "no_rules_dir", "path": str(rules_dir)}) + "\n"
    assert body == expected


def test_rules_audit_missing_header_400(running_daemon):
    _, port = running_daemon
    try:
        _http(port, "/v1/utilization/rules/audit", agent=None)
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_agent_header"
    else:
        raise AssertionError("expected 400 for missing agent header")


# ---------------------------------------------------------------------------
# Byte-compat: guardrails / rb report + candidates (controlled WORLD)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not UTIL_PY.exists(), reason="core/scripts/utilization-stats.py missing")
class TestStoreByteCompat:
    def _check(self, tmp_path, kind, cli_args, handler, query=None):
        from mind_api.src.endpoints import utilization
        gr = _guardrail_records() if kind == "guardrails" else None
        rb = _rb_records() if kind == "rb" else None
        world = _seed_world(tmp_path, "w", gr=gr, rb=rb)
        agent_dir = tmp_path / "agents" / "alpha"
        agent_dir.mkdir(parents=True, exist_ok=True)
        cli_out = _run_cli(world, agent_dir, cli_args).stdout
        resp = handler(_FakeCtx(world=world, agent=agent_dir, query=query))
        assert resp.body.decode("utf-8") == cli_out

    def test_guardrails_report(self, tmp_path):
        from mind_api.src.endpoints import utilization
        self._check(tmp_path, "guardrails", ["guardrails", "report"],
                    utilization.guardrails_report)

    def test_guardrails_candidates_default(self, tmp_path):
        from mind_api.src.endpoints import utilization
        self._check(tmp_path, "guardrails", ["guardrails", "candidates"],
                    utilization.guardrails_candidates)

    def test_guardrails_candidates_limit1(self, tmp_path):
        from mind_api.src.endpoints import utilization
        self._check(tmp_path, "guardrails", ["guardrails", "candidates", "--limit", "1"],
                    utilization.guardrails_candidates, {"limit": "1"})

    def test_guardrails_candidates_limit0_all(self, tmp_path):
        # limit<=0 -> no slicing (truthiness gate) -> all candidates.
        from mind_api.src.endpoints import utilization
        self._check(tmp_path, "guardrails", ["guardrails", "candidates", "--limit", "0"],
                    utilization.guardrails_candidates, {"limit": "0"})

    def test_rb_report(self, tmp_path):
        from mind_api.src.endpoints import utilization
        self._check(tmp_path, "rb", ["rb", "report"], utilization.rb_report)

    def test_rb_candidates_default(self, tmp_path):
        from mind_api.src.endpoints import utilization
        self._check(tmp_path, "rb", ["rb", "candidates"], utilization.rb_candidates)

    def test_empty_world(self, tmp_path):
        from mind_api.src.endpoints import utilization
        world = _seed_world(tmp_path, "w", gr=[], rb=[])
        agent_dir = tmp_path / "agents" / "alpha"
        agent_dir.mkdir(parents=True, exist_ok=True)
        cli_out = _run_cli(world, agent_dir, ["guardrails", "report"]).stdout
        resp = utilization.guardrails_report(_FakeCtx(world=world, agent=agent_dir))
        assert resp.body.decode("utf-8") == cli_out


# ---------------------------------------------------------------------------
# Byte-compat: rules audit
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not UTIL_PY.exists(), reason="core/scripts/utilization-stats.py missing")
class TestRulesAuditByteCompat:
    def test_no_rules_dir_early_return(self, tmp_path):
        # Daemon-side only: compact serialisation, default ensure_ascii=True,
        # NO indent. The CLI can't reach this path (real PROJECT_ROOT has rules).
        from mind_api.src.endpoints import utilization
        empty_root = tmp_path / "empty_repo"
        empty_root.mkdir(parents=True, exist_ok=True)
        resp = utilization.rules_audit(_FakeCtx(
            world=tmp_path / "w", agent=tmp_path / "a", project_root=empty_root))
        rules_dir = empty_root / ".claude" / "rules"
        expected = json.dumps({"status": "no_rules_dir", "path": str(rules_dir)}) + "\n"
        assert resp.body.decode("utf-8") == expected

    def test_main_path_quiescence_retry(self):
        # PROJECT_ROOT (.claude/rules, CLAUDE.md, core/config, sibling agent dirs)
        # is script-pinned to the live repo -> not redirectable for the CLI.
        # A logic bug fails every round; a concurrent agent write fails only
        # some. Pass if any round matches; fail only if all rounds diverge.
        from mind_api.src.endpoints import utilization
        # CLI bound to alpha (AGENT_DIR = agents/alpha, NOT overridden so the
        # sibling loop correctly skips alpha). Daemon ctx mirrors exactly.
        agent_dir = REPO_ROOT / "agents" / "alpha"
        cli_world = _cli_world_dir()   # resolve once (cached) so ctx == CLI
        cli_meta = _cli_meta_dir()
        last_diff = None
        for _ in range(6):
            env = dict(os.environ)
            env["MIND_AGENT"] = "alpha"
            env.pop("MIND_AGENT_DIR", None)  # use real agents/alpha
            proc = subprocess.run(
                [sys.executable, str(UTIL_PY), "rules", "audit"],
                text=True, encoding="utf-8", env=env, cwd=str(REPO_ROOT),
                capture_output=True, timeout=120,
            )
            assert proc.returncode == 0, proc.stderr
            ctx = _FakeCtx(project_root=REPO_ROOT, agent=agent_dir,
                           headers={"x-mind-agent": "alpha"})
            ctx.paths.world = cli_world
            ctx.paths.meta = cli_meta
            resp = utilization.rules_audit(ctx)
            if resp.body.decode("utf-8") == proc.stdout:
                return  # quiescent round matched
            last_diff = (proc.stdout, resp.body.decode("utf-8"))
        # All rounds diverged -> not a transient race; surface the diff.
        cli_out, dmn_out = last_diff
        # Trim to first differing region for a readable failure.
        for i, (c, d) in enumerate(zip(cli_out, dmn_out)):
            if c != d:
                raise AssertionError(
                    f"rules-audit byte-compat diverged at char {i} across 6 rounds:\n"
                    f"CLI : ...{cli_out[max(0,i-60):i+60]!r}\n"
                    f"DMN : ...{dmn_out[max(0,i-60):i+60]!r}")
        raise AssertionError(
            f"rules-audit length mismatch (CLI={len(cli_out)} DMN={len(dmn_out)}) "
            f"across 6 rounds")


def _cli_world_dir():
    """Resolve WORLD_DIR exactly as the CLI's _paths would, for alpha."""
    import importlib.util
    return _load_paths_const("WORLD_DIR")


def _cli_meta_dir():
    return _load_paths_const("META_DIR")


_PATHS_CACHE = {}


def _load_paths_const(name):
    if name in _PATHS_CACHE:
        return _PATHS_CACHE[name]
    env = dict(os.environ)
    env["MIND_AGENT"] = "alpha"
    env.pop("MIND_AGENT_DIR", None)
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import _paths; print(repr(str(_paths.%s)) if _paths.%s is not None else 'None')"
        % (str(REPO_ROOT / "core" / "scripts"), name, name)
    )
    proc = subprocess.run([sys.executable, "-c", code], text=True, env=env,
                          cwd=str(REPO_ROOT), capture_output=True, timeout=30)
    out = proc.stdout.strip()
    val = None if out == "None" else Path(out.strip("'\""))
    _PATHS_CACHE[name] = val
    return val
