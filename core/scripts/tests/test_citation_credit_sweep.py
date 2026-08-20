"""citation-credit-sweep () — hermetic tests.

tmp git repo + tmp world stores + tmp meta ledger; `_rt.store_increment` is
monkeypatched (the sweep imports _rt lazily, so patching the module attribute
intercepts every credit). No daemon, no production stores.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _rt  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "citation_credit_sweep", SCRIPTS / "citation-credit-sweep.py")
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)


def _git(repo: Path, *args):
    r = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(repo), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    world = tmp_path / "world"
    world.mkdir()
    (world / "guardrails.jsonl").write_text(
        json.dumps({"id": "guard-101", "rule": "active guard", "status": "active"}) + "\n"
        + json.dumps({"id": "guard-102", "rule": "retired guard", "status": "retired"}) + "\n",
        encoding="utf-8")
    (world / "reasoning-bank.jsonl").write_text(
        json.dumps({"id": "rb-201", "title": "active rb", "status": "active"}) + "\n",
        encoding="utf-8")
    meta = tmp_path / "meta"
    meta.mkdir()

    calls = []

    def fake_increment(store, rid, field):
        calls.append((store, rid, field))
        return {"ok": True}

    monkeypatch.setattr(_rt, "store_increment", fake_increment)
    monkeypatch.setattr(_rt, "rt_call",
                        lambda *a, **kw: "{}")  # preflight: daemon healthy
    return repo, world, meta, calls


def _run(repo, world, meta, *extra):
    return sweep.main([
        "--repo", str(repo), "--world-dir", str(world),
        "--meta-dir", str(meta), "--force", "--quiet", *extra])


def _ledger_rows(meta):
    p = meta / sweep.LEDGER_NAME
    if not p.exists():
        return []
    return [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_citations_credited_active_only_and_ledgered(env):
    repo, world, meta, calls = env
    _git(repo, "commit", "--allow-empty", "-m",
         "fix(x): apply guard-101's lesson; guard-102 was retired; rb-999 unknown")
    _git(repo, "commit", "--allow-empty", "-m",
         "chore: rb-201 pattern held")
    assert _run(repo, world, meta) == 0
    assert ("guardrails", "guard-101",
            "utilization.times_inferred_helpful") in calls
    assert ("reasoning-bank", "rb-201",
            "utilization.times_inferred_helpful") in calls
    assert all(rid != "guard-102" for _, rid, _f in calls), "retired credited"
    assert all(rid != "rb-999" for _, rid, _f in calls), "unknown credited"
    rows = _ledger_rows(meta)
    assert len(rows) == 2
    by_credit = {tuple(r["credited"]) for r in rows}
    assert ("guard-101",) in by_credit and ("rb-201",) in by_credit
    row1 = next(r for r in rows if r["credited"] == ["guard-101"])
    assert set(row1["skipped_unknown_or_inactive"]) == {"guard-102", "rb-999"}


def test_within_commit_dedup(env):
    repo, world, meta, calls = env
    _git(repo, "commit", "--allow-empty", "-m",
         "guard-101 again guard-101 and once more guard-101")
    _run(repo, world, meta)
    assert len([c for c in calls if c[1] == "guard-101"]) == 1


def test_second_run_is_idempotent(env):
    repo, world, meta, calls = env
    _git(repo, "commit", "--allow-empty", "-m", "uses guard-101")
    _run(repo, world, meta)
    n_calls, n_rows = len(calls), len(_ledger_rows(meta))
    _run(repo, world, meta)
    assert len(calls) == n_calls, "second sweep re-credited a ledgered sha"
    assert len(_ledger_rows(meta)) == n_rows


def test_no_citation_commits_are_not_ledgered(env):
    repo, world, meta, calls = env
    _git(repo, "commit", "--allow-empty", "-m", "plain message, no ids")
    _run(repo, world, meta)
    assert calls == []
    assert _ledger_rows(meta) == []


def test_dry_run_credits_and_ledgers_nothing(env):
    repo, world, meta, calls = env
    _git(repo, "commit", "--allow-empty", "-m", "uses guard-101")
    _run(repo, world, meta, "--dry-run")
    assert calls == []
    assert _ledger_rows(meta) == []


def test_cap_defers_remainder_to_next_sweep(env):
    repo, world, meta, calls = env
    _git(repo, "commit", "--allow-empty", "-m", "first: guard-101")
    _git(repo, "commit", "--allow-empty", "-m", "second: rb-201")
    _run(repo, world, meta, "--max-increments", "1")
    assert len(calls) == 1
    assert len(_ledger_rows(meta)) == 1
    _run(repo, world, meta)  # default cap picks up the deferred commit
    assert len(calls) == 2
    assert len(_ledger_rows(meta)) == 2


def test_mid_sweep_daemon_death_defers_unledgered_remainder(env, monkeypatch):
    """Preflight passes, then the daemon dies mid-sweep: the current row is
    the accepted claim-first loss; every LATER sha stays unledgered and is
    credited by the next sweep."""
    repo, world, meta, calls = env
    _git(repo, "commit", "--allow-empty", "-m", "first: guard-101")
    _git(repo, "commit", "--allow-empty", "-m", "second: rb-201")

    def broken(store, rid, field):
        raise RuntimeError("daemon died mid-sweep")

    monkeypatch.setattr(_rt, "store_increment", broken)
    _run(repo, world, meta)
    assert len(_ledger_rows(meta)) == 1, "later sha must stay unledgered"

    monkeypatch.setattr(
        _rt, "store_increment",
        lambda s, r, f: (calls.append((s, r, f)), {"ok": True})[1])
    _run(repo, world, meta)
    assert ("reasoning-bank", "rb-201",
            "utilization.times_inferred_helpful") in calls


def test_preflight_outage_ledgers_nothing(env, monkeypatch):
    """A daemon that is down BEFORE the sweep starts costs zero credits —
    no ledger rows, full retry next sweep (the retry-burn the preflight
    exists to prevent)."""
    repo, world, meta, calls = env
    _git(repo, "commit", "--allow-empty", "-m", "uses guard-101")

    def down(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(_rt, "rt_call", down)
    _run(repo, world, meta)
    assert _ledger_rows(meta) == [] and calls == []

    monkeypatch.setattr(_rt, "rt_call", lambda *a, **kw: "{}")
    _run(repo, world, meta)
    assert ("guardrails", "guard-101",
            "utilization.times_inferred_helpful") in calls


def test_self_gate_noop_without_force(env):
    repo, world, meta, calls = env
    _git(repo, "commit", "--allow-empty", "-m", "uses guard-101")
    sweep._touch_marker(meta)  # fresh marker
    rc = sweep.main(["--repo", str(repo), "--world-dir", str(world),
                     "--meta-dir", str(meta), "--quiet"])  # no --force
    assert rc == 0
    assert calls == [] and _ledger_rows(meta) == []
