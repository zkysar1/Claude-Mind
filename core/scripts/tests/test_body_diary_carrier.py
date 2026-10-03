"""Tests for the worker-Body diary carrier () and its read-side union.

The defect: `execution-diary.py` appends box-locally to the agent-tree diary and
has NO store delivery in the append path; the agent-tree claim fence refuses a
worker Body's push (the reducer holds the claim); so a worker's rows — including
scorer_override rows — strand box-local and `read_fleet_diaries` (the audit's
roster) is blind to them. Measured 2026-09-28 (g-306-555, alpha worker Body,
cc-07): a Body's local diary held 28 scorer_override rows in the window while
the store held 0, and the audit printed clean over the only force-override.

Seams covered:
 1. body_diary_carrier round-trip (record_local -> read_carrier_lines):
    verbatim lines, per-Body file keying, isolation, fail-open on a missing root.
 2. the worker append hook: a worker Body's `execution-diary.py append` carries
    the row to the carrier (subprocess, house pattern); a non-worker append
    does not; a failing store never fails the diary write.
 3. _fleet_diary.read_fleet_diaries unions the agent's Body carrier rows with
    the agent-wide diary (per-agent text yield, dedupe-safe, no double-count).
 4. scorer-override-audit counts a Body's scorer_override rows via the carrier
    (the live-check seam the audit must no longer be blind to) and the 36h
    default window excludes out-of-window rows.

Hermeticity: tmp_path roots + the conftest `STORAGE_BACKEND=local` pin (guard-955);
`_fleet_diary.WORLD_DIR` patched to a tmp world; nothing touches the live tree.
The subprocess test sets its own env (MIND_AGENT_DIR / MIND_WORLD / MIND_SID)
in the house pattern of test_execution_diary_heartbeat_sync.py.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import body_diary_carrier as bdc  # noqa: E402
import _fleet_diary  # noqa: E402


def _soa():
    """scorer-override-audit.py is hyphenated -> importlib (house pattern)."""
    spec = importlib.util.spec_from_file_location(
        "soa_under_test", SCRIPTS / "scorer-override-audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(offset_h: float) -> str:
    return (datetime.now() - timedelta(hours=offset_h)).strftime("%Y-%m-%dT%H:%M:%S")


def _row(agent: str, claimed: str, top: str, code: str, offset_h: float,
         sid: str | None = None) -> str:
    e = {
        "entry_type": "scorer_override",
        "content": (f"scorer-override: claimed {claimed} over scorer top {top} "
                    f"(deviation={code})"),
        "goal_id": claimed,
        "timestamp": _iso(offset_h),
    }
    if sid is not None:
        e["body_sid"] = sid
    return json.dumps(e, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1. carrier round-trip
# ---------------------------------------------------------------------------

def test_carrier_round_trip_verbatim(tmp_path):
    world = tmp_path / "world"
    rows = [_row("alpha", "g-1", "g-t", "precondition-fail", 30, "sid-A"),
            _row("alpha", "g-2", "g-t", "self-abstention", 20, "sid-A")]
    for r in rows:
        assert bdc.record_local("alpha", "sid-A", r, world_dir=world) is not None

    out = bdc.read_carrier_lines("alpha", world_dir=world)
    assert out == rows, "carrier rows must be VERBATIM (dedup keys on the line)"
    # per-Body keying: another sid of the same agent is isolated
    assert bdc.read_carrier_lines("alpha", world_dir=world) and \
        not (world / "body-diaries" / "alpha" / "sid-B.jsonl").exists()


def test_carrier_isolated_per_agent(tmp_path):
    world = tmp_path / "world"
    bdc.record_local("alpha", "sid-A", _row("alpha", "g-1", "g-t", "force-override", 1, "sid-A"),
                     world_dir=world)
    assert bdc.read_carrier_lines("bravo", world_dir=world) == []


def test_carrier_missing_root_fails_open(tmp_path):
    assert bdc.read_carrier_lines("alpha", world_dir=tmp_path / "nowhere") == []
    assert bdc.record_local("alpha", "sid-A", "", world_dir=tmp_path / "nowhere") is None


# ---------------------------------------------------------------------------
# 2. the worker append hook (subprocess, house pattern)
# ---------------------------------------------------------------------------

def _env(tmp_path, agent_dir: Path, sid: str | None) -> dict:
    env = dict(os.environ)
    env.update(
        MIND_AGENT="alpha",
        MIND_AGENT_DIR=str(agent_dir),
        MIND_WORLD=str(tmp_path / "world"),
        MIND_META=str(tmp_path / "meta"),
        STORAGE_BACKEND="local",
        TZ="UTC",
    )
    if sid is None:
        env.pop("MIND_SID", None)
    else:
        env["MIND_SID"] = sid
    return env


def _run_diary(env: dict, payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "execution-diary.py"), "append"],
        input=payload, text=True, capture_output=True, env=env, timeout=60,
    )


def _mk_worker(agent_dir: Path, sid: str) -> None:
    """A forked worker Body is keyed on sessions/<sid>/working-memory.yaml."""
    (agent_dir / "sessions" / sid).mkdir(parents=True, exist_ok=True)
    (agent_dir / "sessions" / sid / "working-memory.yaml").write_text("{}\n")


def test_worker_append_carries_the_row(tmp_path):
    agent_dir = tmp_path / "agents" / "alpha"
    agent_dir.mkdir(parents=True)
    _mk_worker(agent_dir, "sid-A")
    (tmp_path / "meta").mkdir()
    payload = json.dumps(
        {"entry_type": "scorer_override",
         "content": "scorer-override: claimed g-1 over scorer top g-t (deviation=force-override)"})
    p = _run_diary(_env(tmp_path, agent_dir, "sid-A"), payload)
    assert p.returncode == 0, p.stderr
    local = (agent_dir / "session" / "execution-diary.jsonl").read_text().strip()
    carrier = (tmp_path / "world" / "body-diaries" / "alpha" / "sid-A.jsonl")
    assert carrier.exists(), "worker row must be carried to the Body carrier"
    assert carrier.read_text().strip() == local, "carrier row must be verbatim"
    assert json.loads(local)["body_sid"] == "sid-A"


def test_non_worker_append_does_not_carry(tmp_path):
    agent_dir = tmp_path / "agents" / "alpha"
    agent_dir.mkdir(parents=True)
    (tmp_path / "meta").mkdir()
    payload = json.dumps(
        {"entry_type": "finding", "content": "no body, no carrier"})
    p = _run_diary(_env(tmp_path, agent_dir, None), payload)
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "world" / "body-diaries").exists() is False, \
        "a non-worker Body must never create a carrier"


def test_carry_is_fail_open(tmp_path):
    """A broken store must not fail the diary write (best-effort by contract)."""
    agent_dir = tmp_path / "agents" / "alpha"
    agent_dir.mkdir(parents=True)
    _mk_worker(agent_dir, "sid-A")
    (tmp_path / "meta").mkdir()
    # Make the carrier push fail: a world dir that is a FILE makes the
    # carrier dir mkdir raise (caught -> fail-open), while the local write
    # (under agents/) still succeeds.
    (tmp_path / "world").write_text("not a dir")
    payload = json.dumps(
        {"entry_type": "finding", "content": "carrier down, diary must still write"})
    p = _run_diary(_env(tmp_path, agent_dir, "sid-A"), payload)
    assert p.returncode == 0, f"diary write must survive a carrier failure: {p.stderr}"
    assert (agent_dir / "session" / "execution-diary.jsonl").exists()


# ---------------------------------------------------------------------------
# 3. read_fleet_diaries union
# ---------------------------------------------------------------------------

def _mk_agent(tmp_path: Path, name: str, lines: list[str]) -> None:
    d = tmp_path / name / "session"
    d.mkdir(parents=True, exist_ok=True)
    (d / "execution-diary.jsonl").write_text(
        "\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def _mk_carrier(world: Path, agent: str, sid: str, lines: list[str]) -> None:
    d = world / "body-diaries" / agent
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_fleet_reader_unions_carrier_rows(tmp_path):
    """The UNION lives on the production path (): `base=None`, both
    roots patched to the tmp tree. A non-`None` `base` skips the union — that
    is the hermetic-test seam, and it is pinned in test_fleet_diary.py."""
    agents = tmp_path / "agents"
    world = tmp_path / "world"
    wide = _row("alpha", "g-wide", "g-t", "precondition-fail", 1)
    body = _row("alpha", "g-body", "g-t", "force-override", 2, "sid-A")
    _mk_agent(agents, "alpha", [wide])
    _mk_carrier(world, "alpha", "sid-A", [body])

    with unittest.mock.patch.object(_fleet_diary, "agents_root", lambda: agents), \
         unittest.mock.patch.object(_fleet_diary, "WORLD_DIR", world):
        got = dict(_fleet_diary.read_fleet_diaries())

    # Compare FULL verbatim lines (a goal id occurs twice inside one row, so
    # substring counts are meaningless).
    lines = got["alpha"].splitlines()
    assert body in lines, "Body row must be unioned in"
    assert wide in lines, "agent-wide row must survive verbatim"
    assert len(lines) == 2


def test_fleet_reader_does_not_double_count_identical_rows(tmp_path):
    """A row present in BOTH the agent-wide diary and the carrier is the same
    verbatim line (replay / dual-write); it must count once."""
    agents = tmp_path / "agents"
    world = tmp_path / "world"
    row = _row("alpha", "g-1", "g-t", "force-override", 1, "sid-A")
    _mk_agent(agents, "alpha", [row])
    _mk_carrier(world, "alpha", "sid-A", [row])

    with unittest.mock.patch.object(_fleet_diary, "agents_root", lambda: agents), \
         unittest.mock.patch.object(_fleet_diary, "WORLD_DIR", world):
        got = dict(_fleet_diary.read_fleet_diaries())

    assert got["alpha"].splitlines() == [row], "identical line must count once"


def test_fleet_reader_no_carrier_unchanged(tmp_path):
    agents = tmp_path / "agents"
    world = tmp_path / "world"
    world.mkdir()
    row = _row("alpha", "g-1", "g-t", "self-abstention", 1)
    _mk_agent(agents, "alpha", [row])

    with unittest.mock.patch.object(_fleet_diary, "agents_root", lambda: agents), \
         unittest.mock.patch.object(_fleet_diary, "WORLD_DIR", world):
        got = dict(_fleet_diary.read_fleet_diaries())

    assert got["alpha"].splitlines() == [row]


# ---------------------------------------------------------------------------
# 4. the audit counts Body rows via the carrier
# ---------------------------------------------------------------------------

def test_audit_counts_body_rows_via_carrier(tmp_path):
    soa = _soa()
    agents = tmp_path / "agents"
    world = tmp_path / "world"
    # Agent-wide (reducer) diary: one sanctioned deviation. Body carrier: a
    # force-override — the exact shape of the measured incident (Body over
    # threshold, only force-override in the window, store diary near-clean).
    _mk_agent(agents, "alpha",
              [_row("alpha", f"g-w{i}", f"g-top{i}", "precondition-fail", 1) for i in range(3)])
    _mk_carrier(world, "alpha", "sid-B0",
                [_row("alpha", f"g-b{i}", f"g-btop{i}", "force-override", 2, "sid-B0")
                 for i in range(4)])

    # Production shape (): root=None so the carrier union is live;
    # both roots patched to the tmp tree.
    with unittest.mock.patch.object(_fleet_diary, "agents_root", lambda: agents), \
         unittest.mock.patch.object(_fleet_diary, "WORLD_DIR", world):
        r = soa.audit(since_hours=36)

    assert r["per_agent"]["alpha"]["total"] == 7, "3 reducer + 4 Body rows"
    assert r["per_agent"]["alpha"]["force"] == 4
    assert "alpha" in r["agents_over_threshold"]
    assert r["hits"] is True
    assert r["total_overrides"] == 7


def test_audit_window_excludes_out_of_window_body_rows(tmp_path):
    soa = _soa()
    agents = tmp_path / "agents"
    world = tmp_path / "world"
    _mk_agent(agents, "alpha", [_row("alpha", "g-w", "g-t", "precondition-fail", 1)])
    # 50h old -> OUTSIDE the 36h default (headroom is for delivery lag, not an
    # open-ended lookback).
    _mk_carrier(world, "alpha", "sid-B0",
                [_row("alpha", f"g-b{i}", f"g-btop{i}", "force-override", 50, "sid-B0")
                 for i in range(5)])
    with unittest.mock.patch.object(_fleet_diary, "agents_root", lambda: agents), \
         unittest.mock.patch.object(_fleet_diary, "WORLD_DIR", world):
        r = soa.audit(since_hours=soa.DEFAULT_SINCE_HOURS)
    assert r["total_overrides"] == 1
    assert r["hits"] is False


def test_audit_no_carrier_rows_stays_clean(tmp_path):
    soa = _soa()
    agents = tmp_path / "agents"
    world = tmp_path / "world"
    world.mkdir()
    _mk_agent(agents, "alpha",
              [_row("alpha", "g-w", "g-t", "precondition-fail", 1)])
    # Production shape (): root=None so the carrier union is live;
    # both roots patched to the tmp tree.
    with unittest.mock.patch.object(_fleet_diary, "agents_root", lambda: agents), \
         unittest.mock.patch.object(_fleet_diary, "WORLD_DIR", world):
        r = soa.audit(since_hours=36)
    assert r["total_overrides"] == 1
    assert r["hits"] is False


def test_default_window_is_36():
    assert _soa().DEFAULT_SINCE_HOURS == 36


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
