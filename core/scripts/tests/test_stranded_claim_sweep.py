"""test_stranded_claim_sweep.py — stranded-claim sweep ().

Sweep classifies in-progress claims into kept / stranded based on two signals:
recent execution-diary entry for the goal_id AND age of claimed_at. Tests
cover the classification matrix plus the --apply release path.

Lanes:
  1. Dry-run with no claimed goals → empty stranded list, 0 released
  2. Recent diary entry → KEPT regardless of age
  3. Fresh claim (age < stale_threshold) no diary → KEPT
  4. Old claim (age > stale_threshold) no diary → STRANDED in dry-run
  5. --apply releases stranded: rt_call invoked for release + status update
  6. claimed_at missing → KEPT with diagnostic reason
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


# ---------------------------------------------------------------------------
# Fake _rt — single source of stubbed daemon responses across all tests.
# ---------------------------------------------------------------------------


class _FakeRtError(RuntimeError):
    pass


class _FakeRt:
    """Records rt_call invocations and serves stubbed responses by route."""

    RtError = _FakeRtError

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.responses: Dict[str, Any] = {}
        # The script also calls _rt.aspirations_read (helper, not rt_call).
        # Per-source payloads (: the no-claim scan reads both).
        self._active_payloads: Dict[str, Any] = {}
        # And _rt.tolerant_decode_aggregate (decoder).
        # We pass through dicts unchanged.

    def set_query_response(self, goals: List[Dict[str, Any]]) -> None:
        """The /v1/aspirations/query result for in-progress + claimed_by."""
        self.responses["query"] = json.dumps(goals)

    def set_active_aspirations(self, aspirations: List[Dict[str, Any]],
                               source: str = "both") -> None:
        """The aspirations_read(active=True) payload, per source.

        g-115-2417: the no-claim scan now reads BOTH sources, so payloads are
        stored per source. Default "both" mirrors the old single-payload fake
        for the claimed-path tests (their claimed_at lookups read the entry's
        own source, and their payload goals carry no status so the no-claim
        scan never counts them). No-claim tests pass an explicit source so
        scanned_no_claim counts exactly one scan.
        """
        payload = {"aspirations": aspirations}
        if source == "both":
            self._active_payloads["agent"] = payload
            self._active_payloads["world"] = payload
        else:
            self._active_payloads[source] = payload

    def set_team_in_flight(self, in_flight: Optional[Dict[str, Any]]) -> None:
        """The /v1/team-state/read response for agent_status.<agent>.in_flight.

        Dict-valued fields come back as YAML from the real daemon (it ignores
        any format param — fresh-eyes g-115-2417 live probe); the fake emits
        YAML so the script's decoder is exercised on the real shape.
        """
        if in_flight is None:
            self.responses["team_state"] = "null"
        else:
            import yaml
            self.responses["team_state"] = yaml.safe_dump(in_flight)

    def set_all_agent_status(self, agent_status: Dict[str, Any]) -> None:
        """The /v1/team-state/read response for field=agent_status
        (g-115-2417 world-orphan in_flight guard). YAML — see above."""
        import yaml
        self.responses["team_state_all"] = yaml.safe_dump(agent_status)

    def rt_call(self, method: str, path: str, query=None, body=None, headers=None):
        self.calls.append({
            "method": method, "path": path,
            "query": query, "body": body,
        })
        if path == "/v1/aspirations/query":
            return self.responses.get("query", "[]")
        if path == "/v1/team-state/read":
            if query and query.get("field") == "agent_status":
                return self.responses.get("team_state_all", "{}")
            return self.responses.get("team_state", "null")
        if path == "/v1/aspirations/release":
            if self.responses.get("release_fail"):
                raise _FakeRtError("simulated release failure")
            return json.dumps({"ok": True})
        if path == "/v1/aspirations/update-goal":
            if self.responses.get("update_goal_fail"):
                raise _FakeRtError("simulated update-goal failure")
            return json.dumps({"ok": True})
        return ""

    def aspirations_read(self, source="world", active=False, **kwargs):
        return json.dumps(self._active_payloads.get(source, {"aspirations": []}))

    def tolerant_decode_aggregate(self, source, raw):
        if isinstance(raw, (str, bytes)):
            return json.loads(raw)
        return raw


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_rt(monkeypatch):
    """Replace the imported _rt module attribute on stranded_claim_sweep."""
    sweep = importlib.import_module("stranded-claim-sweep".replace("-", "_"))
    # The module name has hyphens; import via spec to be safe.
    return _import_and_patch_rt(monkeypatch)


def _import_and_patch_rt(monkeypatch):
    """Import the hyphenated script as a module + swap its _rt reference."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "stranded_claim_sweep",
        CORE_SCRIPTS / "stranded-claim-sweep.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    fake = _FakeRt()
    sys.modules["stranded_claim_sweep"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_rt", fake)
    return mod, fake


@pytest.fixture
def tmp_agent(tmp_path, monkeypatch):
    """Create a tmp agent dir + binding, point _paths at it via env."""
    agent_name = "stranded-test-agent"
    agent_dir = tmp_path / "agents" / agent_name
    (agent_dir / "session").mkdir(parents=True)
    diary = agent_dir / "session" / "execution-diary.jsonl"
    diary.write_text("", encoding="utf-8")
    monkeypatch.setenv("MIND_AGENT", agent_name)
    monkeypatch.setenv("MIND_AGENT_DIR", str(agent_dir))
    return agent_name, agent_dir, diary


def _write_diary_entry(diary: Path, goal_id: str, timestamp: str,
                        entry_type: str = "phase_start") -> None:
    rec = {
        "entry_type": entry_type,
        "timestamp": timestamp,
        "goal_id": goal_id,
        "phase": "phase-4-execute",
    }
    with diary.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _patch_agent_dir(monkeypatch, mod, agent_dir: Path) -> None:
    """The script imports `agent_dir` from _paths; route it to our tmp."""
    monkeypatch.setattr(mod, "agent_dir", lambda name: agent_dir)


def _patch_no_bg(monkeypatch, mod) -> None:
    """Default the  bg-pending probe to False so the pre-existing
    keep/release/flip tests exercise the stale+no-diary logic WITHOUT the
    bg-skip guard, and independent of any real `has-pending` subprocess (which
    is otherwise reachable — and, when subprocess.run is stubbed to
    returncode=0 as in the release tests, would misread as 'pending')."""
    monkeypatch.setattr(mod, "_has_pending_background_work", lambda agent: False)


def _run_main(mod, argv: List[str], capsys) -> Dict[str, Any]:
    """Run main() with synthetic argv, parse stdout JSON, return summary dict."""
    old_argv = sys.argv
    sys.argv = ["stranded-claim-sweep.py", *argv]
    try:
        rc = mod.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    out = capsys.readouterr().out
    return json.loads(out)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_claims_clean_dry_run(tmp_agent, monkeypatch, capsys):
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    fake.set_query_response([])

    summary = _run_main(mod, [], capsys)

    assert summary["scanned"] == 0
    assert summary["stranded"] == []
    assert summary["released"] == 0
    assert summary["kept"] == 0
    assert summary["dry_run"] is True


def test_keeps_goal_with_recent_diary(tmp_agent, monkeypatch, capsys):
    """Recent diary entry → KEPT, regardless of how old claimed_at is."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    diary_ts = (claimed_at + dt.timedelta(minutes=2)).isoformat()
    _write_diary_entry(diary, "g-test-001", diary_ts)

    fake.set_query_response([{
        "goal_id": "g-test-001", "asp_id": "asp-test", "source": "world",
        "title": "Test", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-001", "claimed_at": claimed_at.isoformat()}],
    }])

    summary = _run_main(mod, [], capsys)

    assert summary["scanned"] == 1
    assert summary["stranded"] == []
    assert summary["kept"] == 1
    assert summary["released"] == 0


def test_keeps_fresh_claim_with_no_diary(tmp_agent, monkeypatch, capsys):
    """Claim within stale threshold → KEPT even with no diary (race window)."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=2)).replace(microsecond=0)

    fake.set_query_response([{
        "goal_id": "g-test-002", "asp_id": "asp-test", "source": "world",
        "title": "Fresh", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-002", "claimed_at": claimed_at.isoformat()}],
    }])

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    assert summary["scanned"] == 1
    assert summary["stranded"] == []
    assert summary["kept"] == 1


def test_strands_old_claim_no_diary_dry_run(tmp_agent, monkeypatch, capsys):
    """Claim > stale threshold + no diary → STRANDED in dry-run, no release."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)

    fake.set_query_response([{
        "goal_id": "g-test-003", "asp_id": "asp-test", "source": "world",
        "title": "Stranded", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-003", "claimed_at": claimed_at.isoformat()}],
    }])

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    assert summary["scanned"] == 1
    assert summary["kept"] == 0
    assert summary["released"] == 0
    assert len(summary["stranded"]) == 1
    record = summary["stranded"][0]
    assert record["verdict"] == "stranded"
    assert record["goal_id"] == "g-test-003"
    # No release call should have been made in dry-run
    release_calls = [c for c in fake.calls
                     if c["path"] == "/v1/aspirations/release"]
    assert release_calls == []


def test_apply_releases_stranded(tmp_agent, monkeypatch, capsys):
    """--apply triggers release + status update + team-state clear."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)

    fake.set_query_response([{
        "goal_id": "g-test-004", "asp_id": "asp-test", "source": "world",
        "title": "Stranded-apply", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-004", "claimed_at": claimed_at.isoformat()}],
    }])
    # team-state in_flight DOES match → clear path runs
    fake.set_team_in_flight({"goal_id": "g-test-004", "phase": "4"})

    # team-state-clear-in-flight invokes a subprocess; stub it to a no-op.
    class _StubProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_subprocess_run(*args, **kwargs):
        return _StubProc()

    monkeypatch.setattr(mod.subprocess, "run", _fake_subprocess_run)

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    assert summary["kept"] == 0
    assert len(summary["stranded"]) == 1
    record = summary["stranded"][0]
    assert record["verdict"] == "released"
    assert record["release_result"]["ok"] is True
    assert record["team_state_clear"]["cleared"] is True

    # Verify the correct daemon calls were made
    release_calls = [c for c in fake.calls
                     if c["path"] == "/v1/aspirations/release"]
    update_calls = [c for c in fake.calls
                    if c["path"] == "/v1/aspirations/update-goal"]
    assert len(release_calls) == 1
    assert release_calls[0]["query"]["id"] == "g-test-004"
    assert release_calls[0]["query"]["source"] == "world"
    assert len(update_calls) == 1
    assert update_calls[0]["query"]["field"] == "status"
    assert json.loads(update_calls[0]["body"]) == "pending"


def test_claimed_at_missing_kept_with_reason(tmp_agent, monkeypatch, capsys):
    """No claimed_at in the goal record → KEPT with diagnostic reason."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)

    fake.set_query_response([{
        "goal_id": "g-test-005", "asp_id": "asp-test", "source": "world",
        "title": "Missing-claimed_at", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-005"}],  # No claimed_at
    }])

    summary = _run_main(mod, [], capsys)

    assert summary["scanned"] == 1
    assert summary["kept"] == 1
    assert summary["released"] == 0
    # The diagnostic entry is recorded in stranded list with verdict=kept
    assert len(summary["stranded"]) == 1
    record = summary["stranded"][0]
    assert record["verdict"] == "kept"
    assert "claimed_at" in record["reason"]


def test_release_failure_keeps_goal(tmp_agent, monkeypatch, capsys):
    """If aspirations-release fails, goal stays kept + verdict release-failed."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)

    fake.set_query_response([{
        "goal_id": "g-test-006", "asp_id": "asp-test", "source": "world",
        "title": "Release-fail", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-006", "claimed_at": claimed_at.isoformat()}],
    }])
    fake.responses["release_fail"] = True

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 0
    assert summary["kept"] == 1
    record = summary["stranded"][0]
    assert record["verdict"] == "release-failed"
    assert record["release_result"]["ok"] is False
    assert record["release_result"]["step"] == "aspirations-release"


# ---------------------------------------------------------------------------
# : second shape — agent-source in-progress goals with NO claimed_by.
# The claimed_by==agent query is structurally blind to them (agent-source goals
# skip the claim wrapper, the sole claimed_by writer), so the sweep ALSO scans
# the agent-source active aggregate for in-progress + no-claim goals, using
# last_modified as the stale-age basis. Canonical incident:  sat
# in-progress ~4 days with no claimed_by, uncatchable by the old sweep.
# ---------------------------------------------------------------------------


def _no_claim_goal(goal_id, last_modified, status="in-progress", title="No-claim"):
    """An active-aggregate goal record with status but NO claimed_by field."""
    return {"id": goal_id, "status": status,
            "last_modified": last_modified, "title": title}


def test_strands_no_claim_old_inprogress_dry_run(tmp_agent, monkeypatch, capsys):
    """Agent-source in-progress, no claimed_by, stale last_modified, no diary
    → STRANDED (dry-run), shape=no-claim, no daemon write."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])  # no claimed goals — exercise the no-claim path only
    fake.set_active_aspirations([{
        "id": "asp-001",
        "goals": [_no_claim_goal("g-001-02", last_modified.isoformat())],
    }], source="agent")

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    assert summary["scanned"] == 0
    assert summary["scanned_no_claim"] == 1
    assert summary["released"] == 0
    no_claim_recs = [r for r in summary["stranded"] if r.get("shape") == "no-claim"]
    assert len(no_claim_recs) == 1
    assert no_claim_recs[0]["verdict"] == "stranded"
    assert no_claim_recs[0]["goal_id"] == "g-001-02"
    # dry-run: no update-goal write happened
    update_calls = [c for c in fake.calls if c["path"] == "/v1/aspirations/update-goal"]
    assert update_calls == []


def test_keeps_fresh_no_claim(tmp_agent, monkeypatch, capsys):
    """No-claim in-progress within the stale threshold → KEPT (race window)."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=2)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-001",
        "goals": [_no_claim_goal("g-001-09", last_modified.isoformat())],
    }], source="agent")

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    assert summary["scanned_no_claim"] == 1
    assert summary["kept"] == 1
    assert summary["released"] == 0
    assert [r for r in summary["stranded"] if r.get("shape") == "no-claim"] == []


def test_keeps_no_claim_with_recent_diary(tmp_agent, monkeypatch, capsys):
    """No-claim in-progress with a diary entry after last_modified → KEPT
    (work is happening — the diary check carries the primary weight)."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    diary_ts = (last_modified + dt.timedelta(minutes=2)).isoformat()
    _write_diary_entry(diary, "g-001-10", diary_ts)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-001",
        "goals": [_no_claim_goal("g-001-10", last_modified.isoformat())],
    }], source="agent")

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    assert summary["scanned_no_claim"] == 1
    assert summary["kept"] == 1
    assert summary["released"] == 0


def test_apply_flips_no_claim_to_pending(tmp_agent, monkeypatch, capsys):
    """--apply flips a stranded no-claim goal to pending via update-goal; NO
    release call (nothing to release for a never-claimed goal)."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-001",
        "goals": [_no_claim_goal("g-001-02", last_modified.isoformat())],
    }], source="agent")

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    assert summary["kept"] == 0
    rec = [r for r in summary["stranded"] if r.get("shape") == "no-claim"][0]
    assert rec["verdict"] == "released"
    assert rec["flip_result"]["ok"] is True

    # update-goal status=pending was called; NO release call for a no-claim goal
    update_calls = [c for c in fake.calls if c["path"] == "/v1/aspirations/update-goal"]
    release_calls = [c for c in fake.calls if c["path"] == "/v1/aspirations/release"]
    assert len(update_calls) == 1
    assert update_calls[0]["query"]["field"] == "status"
    assert json.loads(update_calls[0]["body"]) == "pending"
    assert release_calls == []


# ---------------------------------------------------------------------------
# : bg-pending guard — mirror of stop-hook Gate 2.5. A claim that
# meets the stale+no-diary criteria may be legitimately paused across a turn
# boundary awaiting REGISTERED background work (OS jobs via background-jobs.sh,
# Claude sub-agents via pending-agents.sh). The sweep skips the release/flip
# and records verdict=kept, reason=stranded-skip-bg, summary.skipped_bg += 1.
# (rb-1533's phase-4 diary marker separately covers the harness-bg-task case.)
# ---------------------------------------------------------------------------


def test_skips_release_when_bg_pending(tmp_agent, monkeypatch, capsys):
    """Claimed path: stale+no-diary claim is KEPT (not released) when the agent
    has pending background work; no /v1/aspirations/release call is made."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    monkeypatch.setattr(mod, "_has_pending_background_work", lambda agent: True)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([{
        "goal_id": "g-test-bg1", "asp_id": "asp-test", "source": "world",
        "title": "Bg-paused", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-bg1", "claimed_at": claimed_at.isoformat()}],
    }])

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["scanned"] == 1
    assert summary["released"] == 0
    assert summary["kept"] == 1
    assert summary["skipped_bg"] == 1
    rec = summary["stranded"][0]
    assert rec["verdict"] == "kept"
    assert "stranded-skip-bg" in rec["reason"]
    release_calls = [c for c in fake.calls if c["path"] == "/v1/aspirations/release"]
    assert release_calls == []


def test_skips_flip_when_bg_pending(tmp_agent, monkeypatch, capsys):
    """No-claim path: a stranded-looking no-claim in-progress goal is KEPT (not
    flipped to pending) when bg work is pending; no update-goal call is made."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    monkeypatch.setattr(mod, "_has_pending_background_work", lambda agent: True)

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-001",
        "goals": [_no_claim_goal("g-001-bg2", last_modified.isoformat())],
    }], source="agent")

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["scanned_no_claim"] == 1
    assert summary["released"] == 0
    assert summary["kept"] == 1
    assert summary["skipped_bg"] == 1
    rec = [r for r in summary["stranded"] if r.get("shape") == "no-claim"][0]
    assert rec["verdict"] == "kept"
    assert "stranded-skip-bg" in rec["reason"]
    update_calls = [c for c in fake.calls if c["path"] == "/v1/aspirations/update-goal"]
    assert update_calls == []


def test_has_pending_background_work_probe(tmp_agent, monkeypatch):
    """The probe returns True iff EITHER has-pending wrapper exits 0 (short-
    circuiting on the first), and is fail-SAFE toward release (False) when the
    subprocess raises."""
    agent_name, agent_dir, diary = tmp_agent
    mod, _fake = _import_and_patch_rt(monkeypatch)

    class _Proc:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = b""
            self.stderr = b""

    # Case A: first wrapper exits 0 → pending → True, short-circuits (1 call).
    calls: List[Any] = []

    def _run_a(cmd, **kw):
        calls.append(cmd)
        return _Proc(0)

    monkeypatch.setattr(mod.subprocess, "run", _run_a)
    assert mod._has_pending_background_work(agent_name) is True
    assert len(calls) == 1

    # Case B: both wrappers exit 1 → not pending → False.
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: _Proc(1))
    assert mod._has_pending_background_work(agent_name) is False

    # Case C: subprocess raises → caught → False (never suppress a release).
    def _run_c(cmd, **kw):
        raise OSError("boom")

    monkeypatch.setattr(mod.subprocess, "run", _run_c)
    assert mod._has_pending_background_work(agent_name) is False


# ---------------------------------------------------------------------------
# Digest-ordering invariant ( / rb-1533)
#
# The sweep's "diary entry after claimed_at → KEPT" heuristic
# (test_keeps_goal_with_recent_diary above) is only sound if a goal that
# reached Phase 4 is GUARANTEED a phase-4-execute diary marker post-dating its
# claim. That guarantee is not in this script — it lives in the loop digest's
# Phase 4 ordering: `execution-diary.sh phase-start phase-4-execute` MUST be
# emitted AFTER `aspirations-claim.sh`.
#
# The original (buggy) order wrote the marker BEFORE the claim, so a
# claim-then-pause (backgrounded tests, stop-hook re-entry) left no diary
# entry after claimed_at and the sweep false-released a legitimately in-flight
# goal (rb-1533). A pre-claim diary window or in_flight match cannot fix this:
# both signals also predate an autocompact orphan and would permanently freeze
# the canonical empty-diary orphan. Reordering the digest so phase-start
# post-dates the claim is the correct fix — phase-start then means "Phase 4
# began", which uniquely discriminates a paused-but-working goal (has marker →
# kept) from an autocompact orphan that never reached Phase 4 (no marker →
# released). This test locks that ordering against regression.
# ---------------------------------------------------------------------------

PROJECT_ROOT = CORE_SCRIPTS.parents[1]
DIGEST = PROJECT_ROOT / "core" / "config" / "aspirations-loop-digest.md"


def _phase_4_block_lines() -> List[str]:
    """Lines of the digest's Phase 4 (claim-conflict gate) block, bounded by
    the `Phase 4.` heading and the next `Phase 4.1.` heading."""
    lines = DIGEST.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.lstrip().startswith("Phase 4.")
                 and "Claim-conflict gate" in ln)
    end = next(i for i, ln in enumerate(lines)
               if i > start and ln.lstrip().startswith("Phase 4.1."))
    return lines[start:end]


def test_digest_writes_phase_start_after_claim():
    """phase-start phase-4-execute MUST appear after aspirations-claim.sh in
    the digest's Phase 4 block (g-115-1371 / rb-1533 regression guard)."""
    block = _phase_4_block_lines()
    claim_idxs = [i for i, ln in enumerate(block) if "aspirations-claim.sh" in ln]
    start_idxs = [i for i, ln in enumerate(block)
                  if "phase-start phase-4-execute" in ln]

    assert len(claim_idxs) == 1, (
        f"expected exactly one aspirations-claim.sh line in the Phase 4 block, "
        f"got {len(claim_idxs)}")
    assert len(start_idxs) == 1, (
        f"expected exactly one `phase-start phase-4-execute` line in the Phase 4 "
        f"block, got {len(start_idxs)}")
    assert start_idxs[0] > claim_idxs[0], (
        "phase-start phase-4-execute must be emitted AFTER aspirations-claim.sh "
        "so a paused-but-claimed goal carries a diary marker post-dating "
        "claimed_at (rb-1533); found phase-start at block-line "
        f"{start_idxs[0]} and claim at block-line {claim_idxs[0]}")


# ---------------------------------------------------------------------------
# : world-source no-claim orphans. The no-claim scan originally
# covered only the agent source ("world goals always claim") — falsified by
# 3 observed world goals stuck in-progress with claimed_by=null. The scan
# now runs for BOTH sources; world entries carry one extra guard: a goal
# named by ANY agent's team-state in_flight is kept (peer mid-execution
# whose claim record was lost).
# ---------------------------------------------------------------------------


def test_apply_flips_world_no_claim_to_pending(tmp_agent, monkeypatch, capsys):
    """WORLD-source stale no-claim orphan → flipped to pending with --apply,
    source=world on the write; no release call."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-115",
        "goals": [_no_claim_goal("g-115-w1", last_modified.isoformat())],
    }], source="world")
    fake.set_all_agent_status({})  # nobody in_flight on it

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["scanned_no_claim"] == 1
    assert summary["released"] == 1
    rec = [r for r in summary["stranded"] if r.get("shape") == "no-claim"][0]
    assert rec["verdict"] == "released"
    assert rec["source"] == "world"
    update_calls = [c for c in fake.calls if c["path"] == "/v1/aspirations/update-goal"]
    release_calls = [c for c in fake.calls if c["path"] == "/v1/aspirations/release"]
    assert len(update_calls) == 1
    assert update_calls[0]["query"]["source"] == "world"
    assert json.loads(update_calls[0]["body"]) == "pending"
    assert release_calls == []


def test_keeps_world_no_claim_when_peer_in_flight(tmp_agent, monkeypatch, capsys):
    """WORLD-source stale no-claim orphan named by a PEER's team-state
    in_flight → KEPT (peer mid-execution, claim record lost); no write."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-115",
        "goals": [_no_claim_goal("g-115-w2", last_modified.isoformat())],
    }], source="world")
    fake.set_all_agent_status({
        "some-peer": {"in_flight": {"goal_id": "g-115-w2", "phase": "4"}},
    })

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["scanned_no_claim"] == 1
    assert summary["released"] == 0
    rec = [r for r in summary["stranded"] if r.get("shape") == "no-claim"][0]
    assert rec["verdict"] == "kept"
    assert "in_flight" in rec["reason"]
    update_calls = [c for c in fake.calls if c["path"] == "/v1/aspirations/update-goal"]
    assert update_calls == []


def test_agent_no_claim_skips_in_flight_guard(tmp_agent, monkeypatch, capsys):
    """AGENT-source no-claim orphans never trigger the team-state
    agent_status read (private queue — no peer can be live on them)."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-001",
        "goals": [_no_claim_goal("g-001-a1", last_modified.isoformat())],
    }], source="agent")

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    all_status_reads = [c for c in fake.calls
                        if c["path"] == "/v1/team-state/read"
                        and c["query"] and c["query"].get("field") == "agent_status"]
    assert all_status_reads == []


def test_world_no_claim_fresh_kept(tmp_agent, monkeypatch, capsys):
    """WORLD-source no-claim orphan inside the stale window → KEPT (also the
    landing side for negative ages from cross-box future stamps, g-115-2418)."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    # Future stamp — a UTC peer's write read on an EDT box (age negative).
    last_modified = (dt.datetime.now() + dt.timedelta(minutes=90)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-115",
        "goals": [_no_claim_goal("g-115-w3", last_modified.isoformat())],
    }], source="world")

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["scanned_no_claim"] == 1
    assert summary["released"] == 0
    assert summary["kept"] == 1
