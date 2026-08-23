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
        # Live server-side team-state row for clear-in-flight ().
        self.team_row: Dict[str, Any] = {}
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

    def set_team_row(self, in_flight: Optional[Dict[str, Any]]) -> None:
        """Seed the SERVER-SIDE row that clear-in-flight mutates ().

        Distinct from set_team_in_flight above, which stubs a READ response.
        This is live state the fake mutates, so a test can assert what the row
        holds AFTER the sweep — the only way to catch a clear that blanked
        someone else's claim.
        """
        self.team_row = {"in_flight": dict(in_flight)} if in_flight else {}

    def set_row_during_release(self, in_flight: Optional[Dict[str, Any]]) -> None:
        """Move the row to `in_flight` when the release call lands ().

        Reproduces the race faithfully instead of pre-seeding the post-race
        state: the row is correct when the sweep forms its verdict and moves
        underneath it, which is exactly the interleaving a check-then-act
        cannot survive.
        """
        self.responses["row_during_release"] = (
            {"in_flight": dict(in_flight)} if in_flight else {})

    def _clear_in_flight(self, query: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v1/team-state/clear-in-flight, via the REAL shared modifier.

        Imports make_clear_in_flight_modifier from _team_state rather than
        re-implementing the compare — the same single-implementation argument
        the production twins make (guard-2323 / guard-547). A hand-mirrored
        CAS in the fake could agree with a broken caller and pass.
        """
        from _team_state import make_clear_in_flight_modifier
        status: Dict[str, Any] = {"cleared": False, "skipped_goal_id": None}
        modifier = make_clear_in_flight_modifier(
            "fake-author",
            if_goal=(query.get("if_goal") or None),
            status=status,
        )
        self.team_row = modifier(self.team_row or {})
        return {"ok": True, "agent": query.get("agent"),
                "cleared": status["cleared"],
                "skipped_goal_id": status["skipped_goal_id"]}

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
            # : lets a test move the team-state row DURING the
            # release — i.e. inside the window between the sweep's ownership
            # verdict and the clear write. That window is the CAS's subject.
            if "row_during_release" in self.responses:
                self.team_row = self.responses.pop("row_during_release")
            return json.dumps({"ok": True})
        if path == "/v1/aspirations/update-goal":
            if self.responses.get("update_goal_fail"):
                raise _FakeRtError("simulated update-goal failure")
            return json.dumps({"ok": True})
        if path == "/v1/pipeline/read":
            # . Default "[]" matches the prior unknown-path "" only
            # because the caller does json.loads(raw or "[]") — being explicit
            # here keeps that fail-open visible rather than incidental.
            return self.responses.get("pipeline", "[]")
        if path == "/v1/team-state/clear-in-flight":
            return json.dumps(self._clear_in_flight(query or {}))
        if path == "/v1/board/post":
            #  / guard-1610. `board_post_fail` exercises the fail-open
            # contract: the queue writes have already landed, so an announce
            # failure must NOT flip the verdict to release-failed.
            if self.responses.get("board_post_fail"):
                raise _FakeRtError("simulated board outage")
            return json.dumps({"ok": True, "id": "msg-fake-0001"})
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
    # team-state in_flight DOES match → the CAS clears it (: the row
    # is live server state now, not a stubbed read — the clear goes through
    # POST /v1/team-state/clear-in-flight, so there is no subprocess to stub).
    fake.set_team_row({"goal_id": "g-test-004", "phase": "4"})

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
    the digest's Phase 4 block (g-115-1371 / rb-1533 regression guard).

    Counts the CALL line only, never a comment that merely names the script
    (g-115-3387, 2026-07-27). The precondition was a bare substring match, so
    prose ABOUT the call counted as another call: g-115-3199 added two
    annotations to this block ("# REDUNDANT -- aspirations-claim.sh
    `_post_claim_effects` stamps ...", "# LOAD-BEARING: agent-queue goals never
    invoke aspirations-claim.sh") and the count went 1 -> 3. The guard's real
    subject -- one call, ordered before phase-start -- was never violated:
    measured call at block-line 28, phase-start at 55. Documenting a call must
    not read as adding one, or the guard punishes the comments that explain it.
    """
    block = _phase_4_block_lines()
    claim_idxs = [i for i, ln in enumerate(block)
                  if "aspirations-claim.sh" in ln
                  and not ln.lstrip().startswith("#")]
    start_idxs = [i for i, ln in enumerate(block)
                  if "phase-start phase-4-execute" in ln]

    assert len(claim_idxs) == 1, (
        f"expected exactly one aspirations-claim.sh CALL line (comment mentions "
        f"excluded) in the Phase 4 block, got {len(claim_idxs)}")
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


# ---------------------------------------------------------------------------
#  — foreign-session guard
#
# The sweep judged a SHARED subject (a claim in aspirations.jsonl) using
# BOX-LOCAL evidence (the execution diary under agents/<agent>/session/, which
# .gitignore excludes and own-cloud keeps machine-local). A second live
# instance of the SAME agent on another box leaves no local diary entry, so its
# LIVE claim read as abandoned and --apply released it out from under a working
# peer. These tests pin both directions: the guard must KEEP a foreign-session
# claim, and must NOT freeze one forever when the holder is genuinely dead.
# ---------------------------------------------------------------------------


class _StubProcOK:
    returncode = 0
    stdout = ""
    stderr = ""


def _stub_subprocess(monkeypatch, mod):
    """team-state clear-in-flight shells out; make it a no-op for release tests."""
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _StubProcOK())


def _claimed_goal_fixture(fake, goal_id, claimed_at, sid=None):
    """One in-progress claimed goal, optionally carrying claimed_by_sid."""
    fake.set_query_response([{
        "goal_id": goal_id, "asp_id": "asp-test", "source": "world",
        "title": "T", "status": "in-progress",
    }])
    goal = {"id": goal_id, "claimed_at": claimed_at.isoformat()}
    if sid is not None:
        goal["claimed_by_sid"] = sid
    fake.set_active_aspirations([{"id": "asp-test", "goals": [goal]}])


def test_keeps_claim_held_by_foreign_session(tmp_agent, monkeypatch, capsys):
    """THE FIX: a claim stamped with ANOTHER session's id is KEPT, even though
    this box has no diary entry for it and the claim is well past stale."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    # 30m old: far past the 5m stale threshold, well inside the 120m grace.
    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    _claimed_goal_fixture(fake, "g-test-fs1", claimed_at, sid="2222-peer-session")

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["scanned"] == 1
    assert summary["released"] == 0, "a live peer instance's claim was released"
    assert summary["kept"] == 1
    assert summary["skipped_foreign_sid"] == 1
    record = summary["stranded"][0]
    assert record["verdict"] == "kept"
    assert "foreign-session" in record["reason"]
    # The destructive call must never have been made.
    assert [c for c in fake.calls if c["path"] == "/v1/aspirations/release"] == []


def test_releases_foreign_session_claim_after_grace(tmp_agent, monkeypatch, capsys):
    """The guard must not be PERMANENT: past the grace window a foreign-session
    claim falls through, so a dead instance cannot freeze a shared goal."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=200)).replace(microsecond=0)
    _claimed_goal_fixture(fake, "g-test-fs2", claimed_at, sid="2222-peer-session")
    fake.set_team_in_flight(None)

    summary = _run_main(
        mod,
        ["--apply", "--stale-minutes", "5", "--foreign-sid-grace-minutes", "120"],
        capsys,
    )

    assert summary["released"] == 1
    assert summary["skipped_foreign_sid"] == 0
    assert summary["stranded"][0]["foreign_sid_grace_expired"] is True


def test_own_session_claim_still_released(tmp_agent, monkeypatch, capsys):
    """The recovery path the sweep EXISTS for must still work: a claim stamped
    with THIS session's id and no diary activity is still stranded."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    _claimed_goal_fixture(fake, "g-test-fs3", claimed_at, sid="1111-this-session")
    fake.set_team_in_flight(None)

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    assert summary["skipped_foreign_sid"] == 0


def test_legacy_claim_without_sid_unchanged(tmp_agent, monkeypatch, capsys):
    """Pre- claims carry no claimed_by_sid — behavior must be
    byte-identical to before the guard (fail-safe toward the old path)."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    _claimed_goal_fixture(fake, "g-test-fs4", claimed_at, sid=None)
    fake.set_team_in_flight(None)

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    assert summary["skipped_foreign_sid"] == 0
    assert summary["stranded"][0]["claimed_by_sid"] is None


def test_absent_local_sid_disengages_guard(tmp_agent, monkeypatch, capsys):
    """No MIND_SID in the environment → the guard disengages entirely.

    Fail-OPEN direction: an unknown local identity must not start keeping
    claims it cannot compare, or the sweep would stop recovering anything.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    monkeypatch.delenv("MIND_SID", raising=False)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    _claimed_goal_fixture(fake, "g-test-fs5", claimed_at, sid="2222-peer-session")
    fake.set_team_in_flight(None)

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["local_sid"] is None
    assert summary["released"] == 1
    assert summary["skipped_foreign_sid"] == 0


def test_apply_logs_released_record_before_mutating(tmp_agent, monkeypatch, capsys):
    """Observability gap (guard-272): after --apply, a follow-up dry-run reports
    0 stranded, so WHICH claim was released must be recorded at release time."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    _claimed_goal_fixture(fake, "g-test-fs6", claimed_at, sid="1111-this-session")
    fake.set_team_in_flight(None)

    old_argv = sys.argv
    sys.argv = ["stranded-claim-sweep.py", "--apply", "--stale-minutes", "5"]
    try:
        assert mod.main() == 0
    finally:
        sys.argv = old_argv
    captured = capsys.readouterr()

    assert "RELEASING" in captured.err
    assert "g-test-fs6" in captured.err
    # The logged payload must be the PRE-mutation record — parseable, and
    # carrying the evidence that justified the release.
    line = [ln for ln in captured.err.splitlines() if "RELEASING" in ln][0]
    payload = json.loads(line.split("RELEASING", 1)[1].strip())
    assert payload["goal_id"] == "g-test-fs6"
    assert payload["claimed_at"] == claimed_at.isoformat()


# ---------------------------------------------------------------------------
# -c — displaced-holder detection (THE LOSER'S HALF)
#
# coordination_merge.py now registers a conflict when two live instances of ONE
# agent claim the same goal, resolving to the older claimed_at. These cover the
# other end: the LOSER learning it was displaced. The whole class was invisible
# because the loser's OWN diary entries satisfy has_recent_diary, so it
# early-returned "work is happening" every iteration and never reached the
# foreign-session guard below — which only ever sees claims with no local diary.
# ---------------------------------------------------------------------------


def _displaced_fixture(fake, diary, goal_id, sid="2222-winner-session"):
    """Foreign-sid claim + diary activity on THIS box after the winner's claim.

    The surviving claimed_at is the WINNER's (older) one — that is what the
    merge writes — and this box worked the goal after it.
    """
    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    _claimed_goal_fixture(fake, goal_id, claimed_at, sid=sid)
    _write_diary_entry(diary, goal_id,
                       (claimed_at + dt.timedelta(minutes=5)).isoformat())
    return claimed_at


def test_displaced_holder_is_detected(tmp_agent, monkeypatch, capsys):
    """Foreign sid + local diary activity => I am the displaced holder."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    claimed_at = _displaced_fixture(fake, diary, "g-test-disp1")

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["possible_displacement"] == 1
    record = summary["stranded"][0]
    assert record["verdict"] == "possible-displacement"
    assert "POSSIBLE DISPLACEMENT" in record["reason"]
    assert record["ambiguous"] is True
    assert record["claimed_by_sid"] == "2222-winner-session"
    assert record["claimed_at"] == claimed_at.isoformat()


def test_displaced_holder_never_releases_the_winners_claim(tmp_agent, monkeypatch,
                                                           capsys):
    """REPORT-ONLY, and this is the load-bearing half of the design.

    After the merge the claim legitimately belongs to the WINNER. Releasing it
    would clear a LIVE holder's claim and re-open the goal to a third instance —
    turning a detected conflict into a wider one. The originating goal's
    description said the loser "aborts/releases"; releasing is wrong, and only
    the abort belongs to the agent reading this verdict.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    _displaced_fixture(fake, diary, "g-test-disp2")

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["possible_displacement"] == 1
    assert summary["released"] == 0
    assert [c for c in fake.calls if c["path"] == "/v1/aspirations/release"] == []


def test_foreign_sid_without_local_diary_is_not_displaced(tmp_agent, monkeypatch,
                                                          capsys):
    """THE DISCRIMINATOR between the two branches.

    Same foreign sid, but NO diary activity on this box => a PEER's claim, not a
    displacement. Must route to the foreign-session guard exactly as before.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    _claimed_goal_fixture(fake, "g-test-disp3", claimed_at, sid="2222-peer-session")
    # deliberately NO diary entry

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["possible_displacement"] == 0
    assert summary["skipped_foreign_sid"] == 1
    assert summary["stranded"][0]["verdict"] == "kept"


def test_own_session_with_diary_is_not_displaced(tmp_agent, monkeypatch, capsys):
    """The healthy steady state: my own sid + my own diary => plain kept."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    _displaced_fixture(fake, diary, "g-test-disp4", sid="1111-this-session")

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["possible_displacement"] == 0
    assert summary["kept"] == 1


def test_legacy_claim_without_sid_is_not_displaced(tmp_agent, monkeypatch, capsys):
    """Pre- claims carry no claimed_by_sid — the branch must disengage
    rather than firing on every legacy claim that has diary activity."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    _displaced_fixture(fake, diary, "g-test-disp5", sid=None)

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["possible_displacement"] == 0
    assert summary["kept"] == 1


def test_absent_local_sid_disengages_displaced_detection(tmp_agent, monkeypatch,
                                                         capsys):
    """Fail-OPEN, matching the foreign-session guard directly below it: an agent
    that cannot read its own identity must not conclude it was displaced."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    monkeypatch.delenv("MIND_SID", raising=False)

    _displaced_fixture(fake, diary, "g-test-disp6")

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["local_sid"] is None
    assert summary["possible_displacement"] == 0
    assert summary["kept"] == 1


# ---------------------------------------------------------------------------
#  — the clear must be a compare-and-swap, not a read-then-blank.
#
# `in_flight` is ONE slot per AGENT. The sweep's mandate is "this goal is
# stranded"; it has none over whatever else the slot names. The old code read
# the row, compared in Python, then issued a clear that blanked whatever was
# present — so a sibling claim landing inside that window was destroyed even
# though the check had "passed". These drive the row across that window.
# ---------------------------------------------------------------------------


def _cas_fixture(fake, claimed_at_minutes: int = 30):
    """Stranded world goal g-test-cas, old enough to release, no diary entry."""
    claimed_at = (dt.datetime.now()
                  - dt.timedelta(minutes=claimed_at_minutes)).replace(microsecond=0)
    fake.set_query_response([{
        "goal_id": "g-test-cas", "asp_id": "asp-test", "source": "world",
        "title": "CAS subject", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-cas", "claimed_at": claimed_at.isoformat()}],
    }])


def test_clear_does_not_blank_a_row_that_moved_to_another_goal(
        tmp_agent, monkeypatch, capsys):
    """The row moves to a SIBLING goal inside the verdict->clear window.

    Mutation proof for the CAS: drop `if_goal` from the clear call in
    stranded-claim-sweep.py and the shared modifier pops on key presence, so
    `fake.team_row` loses in_flight and the final assertion fails. That final
    assertion is the point — a live agent whose in_flight was blanked reads as
    idle to every partner's selector (rb-6498), and nothing in the sweep's own
    output would show it.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    _cas_fixture(fake)
    # Correct at verdict time...
    fake.set_team_row({"goal_id": "g-test-cas", "phase": "4"})
    # ...and moved by a sibling claim while the release is in flight.
    fake.set_row_during_release({"goal_id": "g-test-sibling", "phase": "4"})

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    # The release itself is still correct — only the clear must decline.
    assert summary["released"] == 1
    record = summary["stranded"][0]
    assert record["verdict"] == "released"

    clear = record["team_state_clear"]
    assert clear["cleared"] is False
    assert "g-test-sibling" in clear["reason"], clear["reason"]

    # The CAS travelled on the wire, scoped to the goal the sweep verified.
    clear_calls = [c for c in fake.calls
                   if c["path"] == "/v1/team-state/clear-in-flight"]
    assert len(clear_calls) == 1
    assert clear_calls[0]["query"]["if_goal"] == "g-test-cas"
    assert clear_calls[0]["query"]["agent"] == agent_name

    # THE ASSERTION THAT MATTERS: the sibling's claim survived.
    assert fake.team_row["in_flight"]["goal_id"] == "g-test-sibling"


def test_clear_removes_the_row_when_it_still_holds_the_stranded_goal(
        tmp_agent, monkeypatch, capsys):
    """Positive control: the CAS must not turn the clear into a no-op.

    Without this, a fix that simply stopped clearing anything would satisfy
    the race test above.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    _cas_fixture(fake)
    fake.set_team_row({"goal_id": "g-test-cas", "phase": "4"})

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    assert summary["stranded"][0]["team_state_clear"]["cleared"] is True
    assert "in_flight" not in fake.team_row


def test_clear_reports_absent_row_without_failing_the_release(
        tmp_agent, monkeypatch, capsys):
    """Nothing to clear is a normal outcome, not an error.

    The pre-read used to collapse "absent" and "held by another goal" into one
    reason string ("in_flight did not match goal_id"). The endpoint separates
    them, and the release stands either way.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    _cas_fixture(fake)
    fake.set_team_row(None)

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    clear = summary["stranded"][0]["team_state_clear"]
    assert clear["cleared"] is False
    assert clear["reason"] == "in_flight already absent"


def test_clear_uses_the_daemon_endpoint_not_a_cli_subprocess(
        tmp_agent, monkeypatch, capsys):
    """guard-555 regression guard for the invocation half of .

    The clear used to shell out to `team-state.py clear-in-flight` via
    sys.executable, justified by a docstring claiming no daemon endpoint
    existed. One does. Re-introducing the CLI call would write behind the live
    daemon; this test makes any subprocess in the clear path explode.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)   # the ONLY other subprocess caller

    def _explode(*args, **kwargs):
        raise AssertionError(f"clear path spawned a subprocess: {args!r}")

    monkeypatch.setattr(mod.subprocess, "run", _explode)

    _cas_fixture(fake)
    fake.set_team_row({"goal_id": "g-test-cas", "phase": "4"})

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    assert summary["stranded"][0]["team_state_clear"]["cleared"] is True


# ---------------------------------------------------------------------------
# : LOUD RELEASE (guard-1610). The sweep used to release SILENTLY —
# `_release_goal` / `_flip_pending_no_claim` write the queue only, and the
#  observability is a stderr print, which is ephemeral and box-local.
# So the goal's only surviving board trace stayed the agent's original
# `--type claim` post, unpaired forever: goal-pickup-coordination-check pairs
# claim->release per AUTHOR, and an unpaired claim is a permanent lien.
#
# Measured specimen: world goal  — claimed by alpha 14:28:22, work
# pushed to origin/main 15:56:13Z, released by this sweep 18:53:22 with no
# board post, still `pending` and unclosed 22.3h later. Work-done -> release
# was 2h57m09s; close was never.
# ---------------------------------------------------------------------------


def _board_posts(fake):
    return [c for c in fake.calls if c["path"] == "/v1/board/post"]


def test_world_release_announces_on_the_board(tmp_agent, monkeypatch, capsys):
    """Claimed path, WORLD source → exactly one coordination release post.

    Pins the four fields guard-1610 names literally (channel / type / tags /
    the `RELEASING <id>` text prefix). Two of them are redundant BY DESIGN:
    _released_ids accepts `type=="release"` OR the text prefix OR a
    release-marker tag, so pinning both legs keeps the pairing working even if
    one leg is later re-specified.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([{
        "goal_id": "g-test-loud", "asp_id": "asp-test", "source": "world",
        "title": "Loud-release", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-loud", "claimed_at": claimed_at.isoformat()}],
    }])

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    posts = _board_posts(fake)
    assert len(posts) == 1, "exactly one announcement per release"
    q = posts[0]["query"]
    assert q["channel"] == "coordination"
    assert q["type"] == "release"
    assert q["tags"] == f"g-test-loud,{agent_name}"
    # AUTHOR is load-bearing, not decorative: supersede_released_claims pairs
    # claim->release per author, so a release authored by anyone other than the
    # prior holder cannot clear the lien it exists to clear.
    assert q["author"] == agent_name
    assert posts[0]["body"].startswith("RELEASING g-test-loud")
    rec = summary["stranded"][0]
    assert rec["board_announce"]["posted"] is True


def test_agent_source_release_does_not_announce(tmp_agent, monkeypatch, capsys):
    """AGENT source → NO board post. An agent queue is private: no partner can
    select from it, so a coordination post about one is pure noise. guard-1610
    is scoped to world goals for the same reason."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-001",
        "goals": [_no_claim_goal("g-001-priv", last_modified.isoformat())],
    }], source="agent")

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    assert _board_posts(fake) == []
    rec = [r for r in summary["stranded"] if r.get("shape") == "no-claim"][0]
    assert rec["board_announce"]["posted"] is False
    assert "private" in rec["board_announce"]["reason"]


def test_world_no_claim_flip_announces_as_informational(
        tmp_agent, monkeypatch, capsys):
    """WORLD no-claim flip → announced, and the text says so honestly.

    The claim record is already gone, so no author can be established and the
    post cannot supersede anyone's claim (the pairing helper's own word for a
    release with no preceding claim is "informational"). It is emitted anyway:
    the lien is only half the defect — the other half is that nothing tells the
    fleet the goal became selectable again.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-115",
        "goals": [_no_claim_goal("g-115-orphan", last_modified.isoformat())],
    }], source="world")
    fake.set_all_agent_status({})

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    posts = _board_posts(fake)
    assert len(posts) == 1
    assert posts[0]["body"].startswith("RELEASING g-115-orphan")
    assert "informational only" in posts[0]["body"]


def test_dry_run_never_announces(tmp_agent, monkeypatch, capsys):
    """No --apply → no queue write AND no board post. A dry run that posted
    would announce a release that never happened."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([{
        "goal_id": "g-test-dry", "asp_id": "asp-test", "source": "world",
        "title": "Dry", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-dry", "claimed_at": claimed_at.isoformat()}],
    }])

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    assert summary["released"] == 0
    assert summary["stranded"][0]["verdict"] == "stranded"
    assert _board_posts(fake) == []


def test_announce_failure_does_not_fail_the_release(
        tmp_agent, monkeypatch, capsys):
    """FAIL-OPEN. The queue writes land BEFORE the announcement and are not
    rolled back, so a board outage must leave verdict=released — reporting
    release-failed would be a lie about queue state. The failure is surfaced
    explicitly on the record and on stderr, never swallowed (guard-1534)."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([{
        "goal_id": "g-test-outage", "asp_id": "asp-test", "source": "world",
        "title": "Outage", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-outage", "claimed_at": claimed_at.isoformat()}],
    }])
    fake.responses["board_post_fail"] = True

    # NOT _run_main: it calls capsys.readouterr() itself, which drains BOTH
    # streams and discards .err — the stderr line under test would read as ''.
    old_argv = sys.argv
    sys.argv = ["stranded-claim-sweep.py", "--apply", "--stale-minutes", "5"]
    try:
        assert mod.main() == 0
    finally:
        sys.argv = old_argv
    captured = capsys.readouterr()
    summary = json.loads(captured.out)

    assert summary["released"] == 1
    rec = summary["stranded"][0]
    assert rec["verdict"] == "released"
    assert rec["release_result"]["ok"] is True
    assert rec["board_announce"]["posted"] is False
    assert "simulated board outage" in rec["board_announce"]["error"]
    assert "ANNOUNCE-FAILED g-test-outage" in captured.err


def test_announce_uses_the_daemon_endpoint_not_a_subprocess(
        tmp_agent, monkeypatch, capsys):
    """guard-555 /  regression guard, extended to the announce path.

    POST /v1/board/post is a real daemon route, so shelling out to
    board-post.sh would write behind the live daemon AND re-introduce the
    Python->bash hazard (rb-225/rb-247, guard-580/581) this module removed.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)   # the ONLY other subprocess caller

    def _explode(*args, **kwargs):
        raise AssertionError(f"announce path spawned a subprocess: {args!r}")

    monkeypatch.setattr(mod.subprocess, "run", _explode)

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([{
        "goal_id": "g-test-nosub", "asp_id": "asp-test", "source": "world",
        "title": "NoSub", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-nosub", "claimed_at": claimed_at.isoformat()}],
    }])

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    assert len(_board_posts(fake)) == 1


# ---------------------------------------------------------------------------
#  — body-carrier liveness consult at the foreign-SID grace boundary.
#
# The flat grace above is a pure TIMER: it consults no liveness signal at all,
# so a cross-box worker legitimately running longer than 120m gets its claim
# popped while it is still working. These tests pin BOTH directions, because
# one alone cannot distinguish the fix from a sweep that stopped releasing
# anything: a fresh carrier must HOLD the claim past grace, and a stale one
# must STILL RELEASE it. The two controls (absent, fresh-wrong) pin the
# fail-open direction — every non-`fresh-correct` verdict must reproduce the
# pre-fix behavior byte-for-byte.
#
# The carrier lives under session/ (SINGULAR) and carries a .json suffix for a
# measured reason, not a stylistic one: `sessions` (plural) is in
# owncloud_sync._EXCLUDE_DIRS so nothing under it ever reaches the store, and
# an EXTENSIONLESS basename falls through _session_file_machine_local's suffix
# heuristic to machine_local and silently never leaves the box.
# ---------------------------------------------------------------------------


def _write_carrier(agent_dir: Path, sid: str, age_minutes: float,
                   *, embedded_sid: Optional[str] = None,
                   host: str = "peer-box") -> Path:
    """Write a body-heartbeat carrier for `sid`, aged `age_minutes`.

    `embedded_sid` defaults to `sid`; pass a different value to build the
    guard-358 fresh-but-wrong-writer case. Freshness lives in the CONTENT ts,
    never in mtime — object mtime does not survive the sync, which is the whole
    reason the same-box signal's pure-mtime shape could not be reused here.
    """
    ts = (dt.datetime.now() - dt.timedelta(minutes=age_minutes)).replace(microsecond=0)
    path = agent_dir / "session" / f"body-heartbeat-{sid}.json"
    path.write_text(
        json.dumps({
            "sid": embedded_sid if embedded_sid is not None else sid,
            "agent": "stranded-test-agent",
            "host": host,
            "ts": ts.isoformat(),
        }) + "\n",
        encoding="utf-8",
    )
    return path


def _past_grace_foreign(mod, fake, monkeypatch, goal_id, sid="2222-peer-session"):
    """A foreign-SID claim 200m old — well past the 120m flat grace."""
    monkeypatch.setenv("MIND_SID", "1111-this-session")
    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=200)).replace(microsecond=0)
    _claimed_goal_fixture(fake, goal_id, claimed_at, sid=sid)
    fake.set_team_in_flight(None)


def test_fresh_carrier_holds_a_foreign_claim_past_grace(tmp_agent, monkeypatch,
                                                        capsys):
    """DIRECTION 1 — THE FIX. Grace has expired on the clock, but the holder's
    carrier proves it is alive on another box, so the claim is KEPT."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    _past_grace_foreign(mod, fake, monkeypatch, "g-test-carrier-live")
    _write_carrier(agent_dir, "2222-peer-session", age_minutes=2)

    summary = _run_main(
        mod,
        ["--apply", "--stale-minutes", "5", "--foreign-sid-grace-minutes", "120",
         "--carrier-fresh-minutes", "15"],
        capsys,
    )

    assert summary["released"] == 0, "a demonstrably-live worker's claim was popped"
    assert summary["kept"] == 1
    assert summary["kept_live_carrier"] == 1
    record = summary["stranded"][0]
    assert record["verdict"] == "kept"
    assert record["body_carrier"]["verdict"] == "fresh-correct"
    assert record["body_carrier"]["carrier_host"] == "peer-box"
    assert "foreign_sid_grace_expired" not in record
    # The destructive call must never have been made.
    assert [c for c in fake.calls if c["path"] == "/v1/aspirations/release"] == []


def test_stale_carrier_still_releases_past_grace(tmp_agent, monkeypatch, capsys):
    """DIRECTION 2 — the guard must NOT be a blanket stop-releasing switch.

    Without this test, direction 1 passing is equally consistent with a sweep
    that simply stopped recovering claims. A carrier older than the freshness
    window means the holder is NOT demonstrably alive, so the flat grace
    applies exactly as it did before this block existed.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    _past_grace_foreign(mod, fake, monkeypatch, "g-test-carrier-dead")
    _write_carrier(agent_dir, "2222-peer-session", age_minutes=90)

    summary = _run_main(
        mod,
        ["--apply", "--stale-minutes", "5", "--foreign-sid-grace-minutes", "120",
         "--carrier-fresh-minutes", "15"],
        capsys,
    )

    assert summary["released"] == 1
    assert summary["kept_live_carrier"] == 0
    record = summary["stranded"][0]
    assert record["foreign_sid_grace_expired"] is True
    assert record["body_carrier"]["verdict"] == "stale"


def test_absent_carrier_reproduces_pre_fix_behavior(tmp_agent, monkeypatch,
                                                     capsys):
    """CONTROL — fail-open. No carrier at all (never written, or not yet synced)
    must behave EXACTLY as the sweep did before the consult existed. A carrier
    pipeline that is broken end-to-end can therefore only ever cost the fix's
    benefit, never regress the recovery the sweep exists for.

    `absent` is reported distinctly from `stale` deliberately (guard-2418): the
    two take the same action today, but collapsing them would erase the only
    signal telling a future reader whether the carrier ever arrives at all.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    _past_grace_foreign(mod, fake, monkeypatch, "g-test-carrier-absent")
    # deliberately no _write_carrier call

    summary = _run_main(
        mod,
        ["--apply", "--stale-minutes", "5", "--foreign-sid-grace-minutes", "120"],
        capsys,
    )

    assert summary["released"] == 1
    assert summary["kept_live_carrier"] == 0
    record = summary["stranded"][0]
    assert record["foreign_sid_grace_expired"] is True
    assert record["body_carrier"]["verdict"] == "absent"


def test_fresh_carrier_written_by_a_different_body_does_not_hold(
        tmp_agent, monkeypatch, capsys):
    """CONTROL — guard-358. The carrier is a file-based liveness signal in a
    directory more than one process writes, so freshness ALONE cannot say the
    designated writer is alive: a different body touching a same-named file
    produces an identical fresh reading. The probe therefore compares the
    EMBEDDED writer identity and returns three states, not two.

    A two-state (fresh/stale) probe passes every other test in this block and
    fails only here — which is exactly why this control exists.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    _past_grace_foreign(mod, fake, monkeypatch, "g-test-carrier-wrongsid")
    _write_carrier(agent_dir, "2222-peer-session", age_minutes=1,
                   embedded_sid="3333-someone-else")

    summary = _run_main(
        mod,
        ["--apply", "--stale-minutes", "5", "--foreign-sid-grace-minutes", "120",
         "--carrier-fresh-minutes", "15"],
        capsys,
    )

    assert summary["released"] == 1, "a carrier vouched for a body that did not write it"
    assert summary["kept_live_carrier"] == 0
    record = summary["stranded"][0]
    assert record["body_carrier"]["verdict"] == "fresh-wrong"


def test_fresh_carrier_does_not_hold_a_claim_inside_grace(tmp_agent, monkeypatch,
                                                          capsys):
    """SCOPE — the consult fires ONLY at the grace boundary, never earlier.

    Inside the window the pre-existing foreign-SID keep already applies, and it
    must keep on its own reason (and its own summary field), with no carrier
    read attributed to it. Otherwise the two mechanisms become indistinguishable
    in the record and a future reader cannot tell which one held the claim.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")
    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    _claimed_goal_fixture(fake, "g-test-carrier-early", claimed_at,
                          sid="2222-peer-session")
    _write_carrier(agent_dir, "2222-peer-session", age_minutes=1)

    summary = _run_main(
        mod,
        ["--apply", "--stale-minutes", "5", "--foreign-sid-grace-minutes", "120"],
        capsys,
    )

    assert summary["released"] == 0
    assert summary["kept"] == 1
    assert summary.get("kept_live_carrier", 0) == 0
    record = summary["stranded"][0]
    assert "foreign-session" in record["reason"]
    assert "body_carrier" not in record


def test_carrier_read_goes_through_the_backend_not_the_local_mirror(
        tmp_agent, monkeypatch, capsys):
    """The store-routed read is the ENTIRE reason this design works cross-box,
    and the five tests above do not pin it.

    Proven by mutation, not asserted: raising at the top of the backend block
    leaves all five green, because under STORAGE_BACKEND=local the fallback
    `path.read_text` returns identical bytes. A reducer box must read the
    WORKER's carrier out of the shared store — a local-mirror read returns
    nothing there, and 'nothing' means `absent`, which releases the claim. So
    a silent regression to the local read reintroduces exactly the pop this
    goal exists to prevent, with every test still passing.

    This spies on the backend rather than asserting a return value, because
    the two paths are observationally identical on a single box — the only
    difference that exists to test is WHICH read was made.
    """
    import storage_backend

    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    _past_grace_foreign(mod, fake, monkeypatch, "g-test-carrier-routed")
    carrier = _write_carrier(agent_dir, "2222-peer-session", age_minutes=2)

    seen: List[Path] = []

    class _SpyBackend:
        def read_authoritative_bytes(self, path):
            seen.append(Path(path))
            return Path(path).read_bytes()

    monkeypatch.setattr(storage_backend, "get_backend", lambda: _SpyBackend())

    summary = _run_main(
        mod,
        ["--apply", "--stale-minutes", "5", "--foreign-sid-grace-minutes", "120",
         "--carrier-fresh-minutes", "15"],
        capsys,
    )

    assert seen, "carrier was read WITHOUT consulting the backend — a local-mirror " \
                 "read cannot see a worker box's carrier and would release the claim"
    assert seen[0] == carrier.resolve()
    # The absolute path is load-bearing, not cosmetic: OwnCloudBackend._s3_key
    # raises ValueError outside a configured root and read_authoritative_bytes
    # CATCHES that, silently returning local-mirror bytes from the one method
    # that promises never to read the mirror (_fleet_diary.py docstring).
    assert seen[0].is_absolute()
    record = summary["stranded"][0]
    assert record["body_carrier"]["read_via"] == "authoritative"
    assert record["body_carrier"]["verdict"] == "fresh-correct"
    assert summary["kept_live_carrier"] == 1


# ---------------------------------------------------------------------------
#  — store-of-record diary check on the destructive branch.
#
# The sweep's primary keep-signal (`_diary_has_entry_after`) reads the BOX-LOCAL
# execution-diary.jsonl, but that file is `sync_tier: continuity` and
# `OwnCloudBackend._machine_local()` returns False for it — S3 is authoritative
# and this tree is a read-through cache. So a peer Body's entries are absent
# here until this box pulls, cache-cold reads as "no work is happening", and the
# sweep releases a LIVE worker's claim. These four tests pin the fix AND its
# fail-direction: only a positive AUTHORITATIVE hit may keep, so the three
# non-hit provenances must all still release exactly as before.
# ---------------------------------------------------------------------------


def _patch_authoritative_diary(monkeypatch, result):
    """Seam for the store-of-record read.

    `_diary_has_entry_after_authoritative` does `from _fleet_diary import
    read_agent_diary` INSIDE the function, so the module attribute is what the
    call resolves against. `result` is either a (text, provenance) tuple or a
    callable that may raise.
    """
    import _fleet_diary

    def _fake(agent, base=None, backend="unset"):
        return result(agent) if callable(result) else result

    monkeypatch.setattr(_fleet_diary, "read_agent_diary", _fake)


def _stranded_scenario(fake, goal_id, minutes_ago=30):
    """An old claim with an EMPTY local diary — the release candidate shape."""
    claimed_at = (dt.datetime.now()
                  - dt.timedelta(minutes=minutes_ago)).replace(microsecond=0)
    fake.set_query_response([{
        "goal_id": goal_id, "asp_id": "asp-test", "source": "world",
        "title": "cache-cold", "status": "in-progress",
    }])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": goal_id, "claimed_at": claimed_at.isoformat()}],
    }])
    return claimed_at


def test_cache_cold_local_plus_fresh_authoritative_is_kept(
        tmp_agent, monkeypatch, capsys):
    """THE PIN (): local diary cold + store of record has the entry → KEPT.

    This is the exact live-worker case. Without the fix the local read returns
    False, the claim ages past the stale threshold, and --apply RELEASES a claim
    whose holder is actively working it on another box.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    claimed_at = _stranded_scenario(fake, "g-test-cachecold")
    # The local diary stays EMPTY (tmp_agent writes ""). The worker's entry
    # exists only in the store of record.
    worker_entry = json.dumps({
        "entry_type": "phase_start",
        "timestamp": (claimed_at + dt.timedelta(minutes=1)).isoformat(),
        "goal_id": "g-test-cachecold",
        "phase": "phase-4-execute",
    })
    _patch_authoritative_diary(monkeypatch, (worker_entry + "\n", "authoritative"))

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 0, "a LIVE worker's claim was released"
    assert summary["kept"] == 1
    assert summary["kept_authoritative_diary"] == 1
    record = summary["stranded"][0]
    assert record["verdict"] == "kept"
    # Per-field rather than dict-equality:  added evidence keys
    # (scope / probed / hit_agent) alongside the two load-bearing ones. Pinning
    # the pair by name keeps this strict about what DECIDES the verdict while
    # letting the record carry evidence — and adds a pin the old equality could
    # not express: the claimed path must stay SELF-scoped.
    assert record["authoritative_diary"]["hit"] is True
    assert record["authoritative_diary"]["provenance"] == "authoritative"
    assert record["authoritative_diary"]["scope"] == "self"
    # F-001 arrival counter: the gate was REACHED, not merely non-firing.
    assert summary["authoritative_checks"] == 1
    assert summary["authoritative_provenance"]["authoritative"] == 1
    assert sum(summary["authoritative_provenance"].values()) == \
        summary["authoritative_checks"]
    assert "CACHE-COLD FALSE STRAND" in record["reason"]
    # And nothing was actually released at the daemon.
    assert [c for c in fake.calls if c["path"] == "/v1/aspirations/release"] == []


def test_authoritative_absent_still_releases(tmp_agent, monkeypatch, capsys):
    """NEGATIVE CONTROL: store of record positively reports no diary → RELEASED.

    Without this the pin above would pass on a gate that simply never releases
    anything, which would freeze every genuinely-dead claim in the fleet.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    _stranded_scenario(fake, "g-test-absent")
    fake.set_team_row({"goal_id": "g-test-absent", "phase": "4"})
    _patch_authoritative_diary(monkeypatch, (None, "absent"))

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 1
    assert summary["kept_authoritative_diary"] == 0
    record = summary["stranded"][0]
    assert record["verdict"] == "released"
    assert record["authoritative_diary"]["hit"] is False
    assert record["authoritative_diary"]["provenance"] == "absent"
    assert summary["authoritative_checks"] == 1
    assert summary["authoritative_provenance"]["absent"] == 1


def test_local_mirror_provenance_does_not_keep(tmp_agent, monkeypatch, capsys):
    """Provenance is load-bearing: mirror bytes must NOT keep, even when they match.

    `read_agent_diary` falls back to the local mirror when the backend is absent
    or erroring. Those bytes are the very cache whose coldness caused the bug, so
    treating them as authoritative would launder the local read back in through
    the check written to replace it — and the reason string would claim the store
    of record vouched for a claim it was never asked about (rb-6650).
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    claimed_at = _stranded_scenario(fake, "g-test-mirror")
    matching = json.dumps({
        "entry_type": "phase_start",
        "timestamp": (claimed_at + dt.timedelta(minutes=1)).isoformat(),
        "goal_id": "g-test-mirror",
        "phase": "phase-4-execute",
    })
    # Content that WOULD satisfy the scan — only the provenance differs.
    _patch_authoritative_diary(monkeypatch, (matching + "\n", "local-mirror"))

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    assert summary["kept_authoritative_diary"] == 0
    record = summary["stranded"][0]
    assert record["verdict"] == "stranded"
    assert record["authoritative_diary"]["hit"] is False
    assert record["authoritative_diary"]["provenance"] == "local-mirror"
    assert summary["authoritative_checks"] == 1
    assert summary["authoritative_provenance"]["local-mirror"] == 1


def test_authoritative_probe_error_falls_through_to_prior_behavior(
        tmp_agent, monkeypatch, capsys):
    """A raising probe must not abort the sweep NOR invent a new freeze mode.

    Same idiom as `_body_carrier_verdict`: every non-hit outcome lands on
    exactly the behaviour that shipped before this check existed, so a broken
    backend can only ever cost the sweep its extra safety, never its liveness.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    _stranded_scenario(fake, "g-test-probeerr")

    def _boom(agent):
        raise RuntimeError("S3 unreachable")

    _patch_authoritative_diary(monkeypatch, _boom)

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    assert summary["kept_authoritative_diary"] == 0
    record = summary["stranded"][0]
    assert record["verdict"] == "stranded"
    assert record["authoritative_diary"]["hit"] is False
    assert record["authoritative_diary"]["provenance"] == "error"
    assert summary["authoritative_checks"] == 1
    assert summary["authoritative_provenance"]["error"] == 1


# ---------------------------------------------------------------------------
#  F-002: the WORLD-source no-claim branch probes the FLEET, not self.
#
# `_query_inprogress_no_claim` ignores its `agent` argument entirely, so the
# world half of that scan enumerates in-progress world goals ANY fleet member
# may be mid-execution on — which is why the sibling in_flight guard is already
# fleet-wide. The store-of-record diary probe was self-scoped there, so a goal
# a PEER was working returned `hit=False, provenance=authoritative`: a confident
# miss that licenses the flip. Measured 4/4 on live peer-held goals before the
# fix (/bravo, /echo, /foxtrot, /zeta).
#
# The pair below is a two-way proof. Widening alone is not obviously safe: if
# the fleet scope leaked onto AGENT-source goals, any peer's diary entry for a
# same-named goal would freeze a private goal forever. So one test pins that the
# widening HAPPENS on world, and its twin pins that it does NOT on agent.
# ---------------------------------------------------------------------------


def _patch_fleet_names(monkeypatch, names):
    """Seam for the fleet roster.

    `_authoritative_diary_probe` does `from _fleet_diary import
    fleet_agent_names` INSIDE the function (same idiom as the diary read), so
    the module attribute is what the call resolves against.
    """
    import _fleet_diary
    monkeypatch.setattr(_fleet_diary, "fleet_agent_names", lambda base=None: list(names))


def _peer_only_diary(peer, goal_id, ts):
    """(text, provenance) callable: only `peer` has the entry; self is empty.

    Self returns ("", "absent") — the store of record positively answering "no
    diary here", which is the strongest possible self-miss. If the fix still
    passes under THAT, it cannot be passing because the self read was merely
    unreadable.
    """
    entry = json.dumps({"entry_type": "phase_start", "timestamp": ts,
                        "goal_id": goal_id, "phase": "phase-4-execute"})

    def _read(agent, base=None, backend="unset"):
        if agent == peer:
            return entry + "\n", "authoritative"
        return "", "absent"
    return _read


def test_world_no_claim_probes_peer_diary_and_keeps(tmp_agent, monkeypatch, capsys):
    """WORLD no-claim + entry in a PEER's diary only → KEPT, peer named."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    lm = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-115",
        "goals": [_no_claim_goal("g-115-peerheld", lm.isoformat())],
    }], source="world")
    fake.set_all_agent_status({})  # in_flight guard deliberately does NOT save it

    _patch_fleet_names(monkeypatch, [agent_name, "peer-bravo"])
    _patch_authoritative_diary(monkeypatch, _peer_only_diary(
        "peer-bravo", "g-115-peerheld",
        (lm + dt.timedelta(minutes=1)).isoformat()))

    summary = _run_main(mod, ["--apply", "--stale-minutes", "5"], capsys)

    assert summary["released"] == 0, "flipped a goal a PEER is executing"
    assert summary["kept_authoritative_diary"] == 1
    rec = [r for r in summary["stranded"] if r.get("shape") == "no-claim"][0]
    assert rec["verdict"] == "kept"
    ad = rec["authoritative_diary"]
    assert ad["hit"] is True
    assert ad["scope"] == "fleet"
    assert ad["hit_agent"] == "peer-bravo", "the peer must be NAMED, not just counted"
    assert "peer-bravo" in ad["probed"], "the branch must demonstrably READ the peer"
    assert "peer-bravo" in rec["reason"]
    # No write of any kind reached the daemon.
    assert [c for c in fake.calls if c["path"] == "/v1/aspirations/update-goal"] == []


def test_agent_no_claim_stays_self_scoped(tmp_agent, monkeypatch, capsys):
    """TWIN / ANTI-OVER-WIDENING: an AGENT-source no-claim goal is NOT rescued
    by a peer's diary. Agent-source goals are private, so a peer entry for the
    same id is noise; letting it keep the goal would freeze private work on a
    coincidence. Identical fixture to the test above except the source."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    lm = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-115",
        "goals": [_no_claim_goal("g-115-peerheld", lm.isoformat())],
    }], source="agent")

    _patch_fleet_names(monkeypatch, [agent_name, "peer-bravo"])
    _patch_authoritative_diary(monkeypatch, _peer_only_diary(
        "peer-bravo", "g-115-peerheld",
        (lm + dt.timedelta(minutes=1)).isoformat()))

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    rec = [r for r in summary["stranded"] if r.get("shape") == "no-claim"][0]
    ad = rec["authoritative_diary"]
    assert ad["scope"] == "self", "fleet scope leaked onto the private agent source"
    assert ad["probed"] == [agent_name]
    assert ad["hit"] is False
    assert rec["verdict"] == "stranded"
    assert summary["kept_authoritative_diary"] == 0


def test_fleet_miss_records_weakest_provenance(tmp_agent, monkeypatch, capsys):
    """A fleet MISS is only as trustworthy as its WEAKEST read (guard-980).

    Self answers `absent` (a real answer) while the peer read fails. Recording
    the best provenance would report a confidently-empty store of record when
    one peer was never actually reached — the exact collapse the provenance
    field exists to prevent."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)

    lm = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-115",
        "goals": [_no_claim_goal("g-115-weakprov", lm.isoformat())],
    }], source="world")
    fake.set_all_agent_status({})

    _patch_fleet_names(monkeypatch, [agent_name, "peer-bravo"])

    def _read(agent, base=None, backend="unset"):
        if agent == "peer-bravo":
            raise RuntimeError("S3 unreachable for this peer")
        return "", "absent"

    _patch_authoritative_diary(monkeypatch, _read)

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    ad = [r for r in summary["stranded"]
          if r.get("shape") == "no-claim"][0]["authoritative_diary"]
    assert ad["hit"] is False
    assert ad["provenance"] == "error", "reported the BEST provenance, not the weakest"
    assert summary["authoritative_provenance"]["error"] == 1
    assert summary["authoritative_provenance"]["absent"] == 0
    assert sum(summary["authoritative_provenance"].values()) == \
        summary["authoritative_checks"]


# ── : pending+claimed goals must be reachable ──────────────────────
# The selector deliberately skips a goal claimed by the same mind from another
# Body and its own comment names THIS sweep as the authority that releases it.
# Both of this module's shapes filtered status == in-progress, so a
# pending+claimed goal matched neither and the designated authority could not
# act. Specimen : HIGH, user_directive, deadline the next day, absent
# from all 804 selector candidates.
#
# The shared _FakeRt cannot express this: its rt_call returns ONE stubbed
# payload for every /v1/aspirations/query regardless of goal_status, so a
# regression to a single in-progress-only call would still return the pending
# goal and the test would pass while the defect was back. These use a fake that
# serves BY STATUS, which is the only shape that can tell the two apart.

class _StatusAwareRt:
    """Serves /v1/aspirations/query per goal_status and records what was asked."""

    RtError = _FakeRtError

    def __init__(self, by_status):
        self.by_status = by_status
        self.asked = []

    def rt_call(self, method, path, query=None, body=None, headers=None):
        if path == "/v1/aspirations/query":
            st = (query or {}).get("goal_status")
            self.asked.append(st)
            return json.dumps(self.by_status.get(st, []))
        return "{}"


def test_pending_claimed_goals_are_queried_and_returned(monkeypatch):
    """A pending+claimed goal reaches the sweep, and in-progress still does."""
    mod, _ = _import_and_patch_rt(monkeypatch)
    fake = _StatusAwareRt({
        "in-progress": [{"goal_id": "g-1-1", "source": "world"}],
        "pending":     [{"goal_id": "g-335-731", "source": "world"}],
    })
    monkeypatch.setattr(mod, "_rt", fake)

    got = mod._query_claimed_goals("alpha")
    ids = {g.get("goal_id") for g in got}

    assert "pending" in fake.asked, (
        "the pending status was never queried — a pending+claimed goal is "
        "unreachable and the selector has no release authority (g-115-5052)")
    assert "in-progress" in fake.asked, "the original in-progress path regressed"
    assert ids == {"g-1-1", "g-335-731"}, (
        "expected the union of both statuses, got %r" % (ids,))


def test_pending_claimed_dedups_on_a_status_transition(monkeypatch):
    """The same goal seen in BOTH calls is released once, not twice.

    A goal cannot hold two statuses at rest, but it can transition between the
    two queries. Without dedup that yields a duplicate entry and a second
    release write against a goal already released.
    """
    mod, _ = _import_and_patch_rt(monkeypatch)
    fake = _StatusAwareRt({
        "in-progress": [{"goal_id": "g-dup", "source": "world"}],
        "pending":     [{"goal_id": "g-dup", "source": "world"}],
    })
    monkeypatch.setattr(mod, "_rt", fake)

    got = mod._query_claimed_goals("alpha")
    assert [g.get("goal_id") for g in got] == ["g-dup"], (
        "a goal appearing in both status queries must be returned ONCE")

# ===========================================================================
#  — completed-not-closed guard
#
# The defect: every gate in this sweep asks "is anyone WORKING on this now?"
# and none asked "did the work already HAPPEN?". A worker Body that finishes a
# goal is correctly idle, so it looked abandoned on every liveness signal — and
# one --apply run released 32 goals of which 22 were finished work (notes
# 3,723-13,693 chars, several naming MERGED PRs).
#
# Every test below was proven to FAIL against the pre-fix module before being
# trusted (guard-1988); the two negative controls are what stop the new
# predicate from silently disabling the sweep.
# ===========================================================================


_BIG_NOTE = (
    "DONE — all outcomes met. Held at in-progress for the reducer.\n"
    + ("x" * 4000)
)


def _stale_claim_setup(mod, fake, monkeypatch, agent_dir, goal_extra):
    """One stale claimed goal that every liveness gate reads as abandoned."""
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([{
        "goal_id": "g-test-5177", "asp_id": "asp-test", "source": "world",
        "title": "completed-not-closed subject", "status": "in-progress",
    }])
    goal = {"id": "g-test-5177", "claimed_at": claimed_at.isoformat()}
    goal.update(goal_extra)
    fake.set_active_aspirations([{"id": "asp-test", "goals": [goal]}])


def test_completed_not_closed_note_keeps_and_quotes_first_line(
        tmp_agent, monkeypatch, capsys):
    """POSITIVE: a big outcome_note KEEPS the goal and surfaces its verdict line.

    The quoted first line is not decoration. guard-2852(c): of six phantom-
    pending goals measured, five notes said DONE and one 5,701-char note opened
    "ACCEPTANCE NOT MET — do NOT close this". Length is not verdict, so the
    record must hand the next reader the line that IS the verdict.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir,
                       {"outcome_note": _BIG_NOTE})

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    record = summary["stranded"][0]
    assert record["verdict"] == "completed-not-closed"
    assert summary["kept"] == 1
    assert summary["released"] == 0
    assert summary["kept_completed_not_closed"] == 1
    assert summary["completion_checks"] == 1
    assert record["completion_evidence"]["predicate"] == "outcome_note"
    # The FIRST LINE specifically — not a prefix of the blob, which would be
    # the same string here only by accident of fixture construction.
    assert record["completion_evidence"]["note_head"] == \
        "DONE — all outcomes met. Held at in-progress for the reducer."
    assert "STOP AND READ" in record["reason"]
    assert "LENGTH IS NOT VERDICT" in record["reason"]


# --- production-shape note_head regression () --------------------
# The fixture above opens with a bare verdict line. THE LIVE POPULATION DOES
# NOT: measured 2026-08-15 over all 334 candidates (alpha, hostname cc-04,
# uname -r 6.8.0-137-generic, own-cloud), 180 (53.9%) open with a worker-Body
# provenance stamp and 92 of those carry no verdict word anywhere in line 1.
# So the assertion above passed for months while the field it pins was blank
# for the majority of real goals — the production shape is precisely what the
# fixture failed to replicate (guard-920). These tests carry that shape.
_PROVENANCE_NOTE = (
    "alpha worker Body @ hostname cc-07, uname -r 6.8.0-136-generic, "
    "2026-08-08T06:46-07:2x.\n"
    "BUILT AND LIVE. 4 commits: 633400693, 9dd024191, aa61605fb, 6dda7720f.\n"
    "Held in-progress for the reducer; completed_by unset.\n"
    + ("x" * 4000)
)


def test_note_head_skips_provenance_stamp_and_quotes_the_verdict(
        tmp_agent, monkeypatch, capsys):
    """The verbatim production shape: provenance on line 1, verdict on line 2."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir,
                       {"outcome_note": _PROVENANCE_NOTE})

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    record = summary["stranded"][0]
    head = record["completion_evidence"]["note_head"]
    # The verdict, not the box identity.
    assert head == ("BUILT AND LIVE. 4 commits: 633400693, 9dd024191, "
                    "aa61605fb, 6dda7720f.")
    assert "worker Body" not in head and "uname -r" not in head
    # The KEEP decision is unchanged — this field is display-only.
    assert record["verdict"] == "completed-not-closed"
    assert summary["kept_completed_not_closed"] == 1
    assert summary["released"] == 0


def test_verdict_head_falls_back_and_never_returns_empty(monkeypatch):
    """Fallback contract: never empty, never LESS than the old behaviour."""
    mod, _ = _import_and_patch_rt(monkeypatch)
    # No provenance → unchanged (the pre-existing behaviour is preserved).
    assert mod._verdict_head("DONE.\nbody") == "DONE."
    # Leading blank lines are skipped, as before.
    assert mod._verdict_head("\n\n  DONE.  \nbody") == "DONE."
    # All-provenance → falls back to line 1 rather than returning nothing.
    allprov = ("alpha worker Body @ cc-07\nhostname cc-07\nuname -r 6.8.0\n"
               "uname -r 6.8.0\n")
    assert mod._verdict_head(allprov) == "alpha worker Body @ cc-07"
    # Empty / whitespace-only stays empty.
    assert mod._verdict_head("") == ""
    assert mod._verdict_head("\n  \n") == ""
    # The skip is BOUNDED: a 4th provenance line is not scanned past.
    assert mod._verdict_head(allprov + "REAL VERDICT") == "alpha worker Body @ cc-07"


def test_negative_control_no_note_still_strands(tmp_agent, monkeypatch, capsys):
    """NEGATIVE CONTROL: no evidence → the sweep still works.

    Without this, a predicate that returned "keep" unconditionally would pass
    every positive test above while silently disabling the whole sweep.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir, {})

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    record = summary["stranded"][0]
    assert record["verdict"] == "stranded"
    assert summary["kept_completed_not_closed"] == 0
    # The gate was REACHED and declined — not skipped. Distinguishing those is
    # the whole reason the two counters are published as a pair.
    assert summary["completion_checks"] == 1


def test_negative_control_short_note_still_strands(tmp_agent, monkeypatch, capsys):
    """NEGATIVE CONTROL: a stub note is not evidence of completed work."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir,
                       {"outcome_note": "started looking at this"})

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)
    assert summary["stranded"][0]["verdict"] == "stranded"
    assert summary["kept_completed_not_closed"] == 0


@pytest.mark.parametrize("marker,value", [
    ("recurring", True),
    ("interval_hours", 168),
    ("recurring_interval_hours", 10.67),
    ("original_interval_hours", 24),
])
def test_recurring_goal_with_big_note_is_not_kept(
        tmp_agent, monkeypatch, capsys, marker, value):
    """A standing cadence's note is the PREVIOUS cycle's — never this one's.

    Keeping these would freeze every recurring goal in the fleet permanently.
    Measured live: 2 of 42 note-bearing in-progress goals are recurring, and
    they carry the marker under DIFFERENT field names — so each name is pinned
    separately. Dropping any one of them silently re-opens the freeze for that
    subset, which no single-marker test would catch.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir,
                       {"outcome_note": _BIG_NOTE, marker: value})

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    assert summary["stranded"][0]["verdict"] == "stranded"
    assert summary["kept_completed_not_closed"] == 0


def test_recurring_falsey_marker_does_not_exempt(tmp_agent, monkeypatch, capsys):
    """`recurring: False` / `interval_hours: 0` must NOT read as recurring.

    A truthiness bug here would silently release finished non-recurring work —
    the original defect — so the falsey case is pinned explicitly.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir,
                       {"outcome_note": _BIG_NOTE,
                        "recurring": False, "interval_hours": 0})

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)
    assert summary["stranded"][0]["verdict"] == "completed-not-closed"


def test_pipeline_resolution_reference_keeps_note_less_goal(
        tmp_agent, monkeypatch, capsys):
    """POSITIVE: the second predicate catches the goal whose note was never written."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir, {})
    fake.responses["pipeline"] = json.dumps([
        {"id": "2026-07-27_some-hypothesis", "stage": "resolved",
         "outcome": "CORRECTED", "resolved_goal": "g-test-5177"},
    ])

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    record = summary["stranded"][0]
    assert record["verdict"] == "completed-not-closed"
    assert record["completion_evidence"]["predicate"] == "pipeline_resolution_ref"
    assert summary["kept_completed_not_closed"] == 1


@pytest.mark.parametrize("field", [
    "resolved_goal", "resolution_goal", "resolved_in_goal", "resolved_by_goal",
])
def test_all_four_resolution_field_names_are_honoured(
        tmp_agent, monkeypatch, capsys, field):
    """The originating goal named ONE field; it covers 1 of 56 live records.

    `resolved_goal` alone would have been ~2% effective while reading as a
    working predicate. Each of the four resolution-side names is pinned, so
    dropping one cannot pass this suite.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir, {})
    fake.responses["pipeline"] = json.dumps([
        {"id": "h", "stage": "resolved", field: "g-test-5177"}])

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)
    assert summary["stranded"][0]["verdict"] == "completed-not-closed"


@pytest.mark.parametrize("field", ["source_goal", "origin_goal"])
def test_formation_side_reference_does_not_keep(
        tmp_agent, monkeypatch, capsys, field):
    """NEGATIVE CONTROL: forming a hypothesis is not resolving it.

    `source_goal` is on 48 of 56 resolved records — including it would keep
    nearly every goal that ever filed a hypothesis, freezing genuinely
    stranded work. The originating specimen proves the split by itself:
    hypothesis 2026-07-27_position-fix-moves-failure-downstream carries
    source_goal=g-335-319 but resolved_goal=g-335-326, so a source_goal
    predicate would have kept the WRONG goal and still released the one the
    whole filing was about. In all 9 live records carrying both, they differ.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir, {})
    fake.responses["pipeline"] = json.dumps([
        {"id": "h", "stage": "resolved", field: "g-test-5177"}])

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)
    assert summary["stranded"][0]["verdict"] == "stranded"
    assert summary["kept_completed_not_closed"] == 0


def test_apply_does_not_release_completed_not_closed(
        tmp_agent, monkeypatch, capsys):
    """--apply must make NO release call. Dry-run agreement is not enough.

    The verdict is computed in both modes deliberately, but only --apply
    mutates — so the mutation path needs its own proof that nothing fired.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir,
                       {"outcome_note": _BIG_NOTE})

    summary = _run_main(mod, ["--stale-minutes", "5", "--apply"], capsys)

    assert summary["released"] == 0
    assert [c for c in fake.calls if c["path"] == "/v1/aspirations/release"] == []
    assert [c for c in fake.calls
            if c["path"] == "/v1/aspirations/update-goal"] == []


def test_threshold_flag_is_honoured(tmp_agent, monkeypatch, capsys):
    """The threshold is tunable, and raising it past the note releases again."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir,
                       {"outcome_note": _BIG_NOTE})

    summary = _run_main(
        mod, ["--stale-minutes", "5", "--completion-note-min-chars", "999999"],
        capsys)
    assert summary["stranded"][0]["verdict"] == "stranded"


def test_no_claim_branch_is_also_guarded(tmp_agent, monkeypatch, capsys):
    """The second release path flips in-progress -> pending just as destructively.

    A goal whose claim record was stripped is not less finished than one that
    kept it, and this branch is what the g-115-1691 / g-115-2417 shapes reach.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    lm = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)

    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [{"id": "g-test-5177nc", "status": "in-progress",
                   "last_modified": lm.isoformat(),
                   "outcome_note": _BIG_NOTE}],
    }], source="world")

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)

    nc = [r for r in summary["stranded"] if r.get("shape") == "no-claim"]
    assert len(nc) == 1, f"expected one no-claim record, got {summary['stranded']}"
    assert nc[0]["verdict"] == "completed-not-closed"
    assert summary["kept_completed_not_closed"] == 1


def test_pipeline_read_failure_fails_open_to_release(
        tmp_agent, monkeypatch, capsys):
    """A broken pipeline read must not start KEEPING everything.

    Fail-open direction: a missed keep costs one re-claimable goal; a blanket
    keep would freeze the queue. Mirrors `_local_sid`'s stated fail direction.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _stale_claim_setup(mod, fake, monkeypatch, agent_dir, {})
    fake.responses["pipeline"] = "{ this is not json"

    summary = _run_main(mod, ["--stale-minutes", "5"], capsys)
    assert summary["stranded"][0]["verdict"] == "stranded"


# ---------------------------------------------------------------------------
#  — per-run carrier memo.
#
# The sweep's wall clock is O(this agent's outstanding claims) x one REMOTE
# round trip per foreign-sid claim past grace, and nothing deduped those reads.
# Measured 2026-08-13 (alpha, cc-04, the BACKGROUNDED --apply run — the mode is
# load-bearing, see the sweep's own memo comment): alpha held 318 claims carrying
# only SIX distinct claimed_by_sid values, so 307 probes resolved to 6 distinct
# files and the sweep timed out as an ALWAYS-RUN entry call. These tests pin the
# dedup in both directions — it must collapse repeats AND must not merge
# distinct sids.
#
# `carrier_reads` is what the wall clock tracks; `carrier_checks` is what the
# logic needed. Asserting only the first would pass against a memo that had
# been removed, so both are pinned in one test.
# ---------------------------------------------------------------------------


def _many_claimed_goals(fake, specs, claimed_at):
    """N in-progress claimed goals: `specs` is [(goal_id, sid), ...]."""
    fake.set_query_response([
        {"goal_id": gid, "asp_id": "asp-test", "source": "world",
         "title": "T", "status": "in-progress"}
        for gid, _sid in specs
    ])
    fake.set_active_aspirations([{
        "id": "asp-test",
        "goals": [
            {"id": gid, "claimed_at": claimed_at.isoformat(), "claimed_by_sid": sid}
            for gid, sid in specs
        ],
    }])


def test_carrier_probe_is_memoized_per_sid_within_one_run(
        tmp_agent, monkeypatch, capsys):
    """Repeat sids cost ONE remote read; distinct sids still cost one each."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=200)).replace(microsecond=0)
    _many_claimed_goals(fake, [
        ("g-memo-1", "aaaa-peer"),
        ("g-memo-2", "aaaa-peer"),
        ("g-memo-3", "aaaa-peer"),
        ("g-memo-4", "aaaa-peer"),
        ("g-memo-5", "bbbb-peer"),
    ], claimed_at)
    fake.set_team_in_flight(None)
    # Fresh carriers for BOTH sids: every claim is kept, so this test measures
    # the probe accounting and never the release path.
    _write_carrier(agent_dir, "aaaa-peer", age_minutes=2)
    _write_carrier(agent_dir, "bbbb-peer", age_minutes=2)

    summary = _run_main(
        mod,
        ["--apply", "--stale-minutes", "5", "--foreign-sid-grace-minutes", "120",
         "--carrier-fresh-minutes", "15"],
        capsys,
    )

    assert summary["scanned"] == 5
    # Every claim still NEEDED a verdict — the memo must not skip the logic.
    assert summary["carrier_checks"] == 5
    # ...but only two distinct (sid, fresh_minutes) keys were ever fetched.
    assert summary["carrier_reads"] == 2, (
        "expected one remote read per distinct sid; got "
        f"{summary['carrier_reads']} — memo removed or keyed wrongly"
    )
    # Positive control: without the memo these are equal. This is the assertion
    # that actually fails if the dedup is reverted.
    assert summary["carrier_reads"] < summary["carrier_checks"]
    # The dedup must not change any VERDICT: all five are still held.
    assert summary["kept_live_carrier"] == 5
    assert summary["released"] == 0
    verdicts = {r["goal_id"]: r["body_carrier"]["verdict"]
                for r in summary["stranded"]}
    assert set(verdicts.values()) == {"fresh-correct"}
    assert len(verdicts) == 5


def test_carrier_memo_reports_reads_equal_to_checks_when_all_sids_differ(
        tmp_agent, monkeypatch, capsys):
    """CONTROL — the memo must not under-report on a worst case.

    With no repeated sid there is nothing to save, and `carrier_reads` must
    equal `carrier_checks`. Without this, a memo that wrongly collapsed
    DISTINCT sids to one read would still pass the test above's `<` assertion
    while silently applying one body's liveness verdict to another's claim.
    """
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    _patch_no_bg(monkeypatch, mod)
    _stub_subprocess(monkeypatch, mod)
    monkeypatch.setenv("MIND_SID", "1111-this-session")

    claimed_at = (dt.datetime.now() - dt.timedelta(minutes=200)).replace(microsecond=0)
    _many_claimed_goals(fake, [
        ("g-memo-d1", "cccc-peer"),
        ("g-memo-d2", "dddd-peer"),
        ("g-memo-d3", "eeee-peer"),
    ], claimed_at)
    fake.set_team_in_flight(None)
    for sid in ("cccc-peer", "dddd-peer", "eeee-peer"):
        _write_carrier(agent_dir, sid, age_minutes=2)

    summary = _run_main(
        mod,
        ["--apply", "--stale-minutes", "5", "--foreign-sid-grace-minutes", "120",
         "--carrier-fresh-minutes", "15"],
        capsys,
    )

    assert summary["carrier_checks"] == 3
    assert summary["carrier_reads"] == 3
    assert summary["kept_live_carrier"] == 3


# ── constant calibration: the carrier window vs the cadence that WRITES it ──
#
# . DEFAULT_CARRIER_FRESH_MINUTES is a freshness bound on a signal
# written once per worker cycle, so it must bracket that cycle from BOTH sides.
# Pinned here rather than left to prose because a config value and the prose
# justifying it are two artifacts that drift apart silently (guard-4282): this
# flag's help asserted the carrier "refreshes continuously" while its only
# worker call site was the top of each cycle, and the window sat at the very
# bottom of the measured gap distribution for as long as that went unchecked.

_WORKER_CYCLE_GAP_MAX_MIN = 92  # derived; re-anchored by the test below


def _load_sweep_constants():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_scs_constants", CORE_SCRIPTS / "stranded-claim-sweep.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_carrier_window_brackets_the_worker_cycle_cadence():
    mod = _load_sweep_constants()
    window = mod.DEFAULT_CARRIER_FRESH_MINUTES
    grace = mod.DEFAULT_FOREIGN_SID_GRACE_MINUTES

    # LOWER bound. Below the longest worker cycle gap, the keep-veto reports a
    # LIVE worker stale and its claim falls through to release mid-execution —
    # the  pop this veto was added () to prevent.
    assert window > _WORKER_CYCLE_GAP_MAX_MIN, (
        f"carrier window {window}m does not cover the measured worker cycle "
        f"gap ({_WORKER_CYCLE_GAP_MAX_MIN}m). A worker mid-unit writes no "
        f"carrier, so it would be judged stale while actively working."
    )

    # UPPER bound. At or past the grace the grace is unreachable, so a
    # genuinely dead holder's claim would never fall through to release.
    assert window < grace, (
        f"carrier window {window}m must stay below the foreign-sid grace "
        f"{grace}m or the grace becomes unreachable."
    )


def test_worker_cycle_gap_figure_still_matches_its_source():
    """The window above is DERIVED from a measurement recorded in the worker
    loop. If that measurement is revised, fail here so the derived window is
    revisited rather than left calibrated against a number nobody re-checked.
    """
    import re

    skill = PROJECT_ROOT / ".claude" / "skills" / "worker-loop" / "SKILL.md"
    text = skill.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"measured unit gaps of (\d+)-(\d+) min", text)
    assert m, (
        f"the 'measured unit gaps of N-M min' anchor is gone from {skill}. "
        f"Re-anchor _WORKER_CYCLE_GAP_MAX_MIN before trusting "
        f"DEFAULT_CARRIER_FRESH_MINUTES — the window is derived from it."
    )
    assert int(m.group(2)) == _WORKER_CYCLE_GAP_MAX_MIN, (
        f"worker cycle gap upper bound moved to {m.group(2)}m; "
        f"re-derive DEFAULT_CARRIER_FRESH_MINUTES against it."
    )


# ---------------------------------------------------------------------------
# Parked in-progress detector (fourth shape, 2026-08-21 selection-stack review)
# ---------------------------------------------------------------------------


def test_parked_inprogress_detected(tmp_agent, monkeypatch, capsys):
    """in-progress + defer_reason (the  shape) is reported by the
    detect-only leg — even when claimed by ANOTHER agent, since detection is
    read-only and the park is invisible to both selector surfaces."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-park", "status": "active",
        "goals": [
            {"id": "g-park-01", "status": "in-progress",
             "claimed_by": "someone-else",
             "defer_reason": "blocked_on_dependency g-park-99 — wrong box",
             "last_modified": "2026-08-15T06:38:46"},
            {"id": "g-park-02", "status": "pending",
             "defer_reason": "precondition_unmet:window"},
            {"id": "g-park-03", "status": "in-progress",
             "claimed_by": agent_name},
        ],
    }], source="agent")

    summary = _run_main(mod, [], capsys)

    parked = summary["parked_in_progress"]
    assert summary["parked_in_progress_count"] == 1, summary
    assert parked[0]["goal_id"] == "g-park-01"
    assert parked[0]["claimed_by"] == "someone-else"
    assert parked[0]["defer_head"].startswith("blocked_on_dependency")
    # detect-only: the leg must not have released or flipped anything
    assert summary["released"] == 0


def test_parked_counter_published_when_clean(tmp_agent, monkeypatch, capsys):
    """Paired-counter contract: a clean run publishes parked_in_progress_count
    == 0 (a reader must be able to tell 'no parks' from 'leg never ran')."""
    agent_name, agent_dir, diary = tmp_agent
    mod, fake = _import_and_patch_rt(monkeypatch)
    _patch_agent_dir(monkeypatch, mod, agent_dir)
    fake.set_query_response([])

    summary = _run_main(mod, [], capsys)

    assert summary["parked_in_progress"] == []
    assert summary["parked_in_progress_count"] == 0
