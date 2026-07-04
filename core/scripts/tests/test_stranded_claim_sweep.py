"""test_stranded_claim_sweep.py — stranded-claim sweep (4).

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
        self._active_payload: Any = []
        # And _rt.tolerant_decode_aggregate (decoder).
        # We pass through dicts unchanged.

    def set_query_response(self, goals: List[Dict[str, Any]]) -> None:
        """The /v1/aspirations/query result for in-progress + claimed_by."""
        self.responses["query"] = json.dumps(goals)

    def set_active_aspirations(self, aspirations: List[Dict[str, Any]]) -> None:
        """The aspirations_read(active=True) payload."""
        self._active_payload = {"aspirations": aspirations}

    def set_team_in_flight(self, in_flight: Optional[Dict[str, Any]]) -> None:
        """The /v1/team-state/read response for agent_status.<agent>.in_flight."""
        if in_flight is None:
            self.responses["team_state"] = "null"
        else:
            self.responses["team_state"] = json.dumps(in_flight)

    def rt_call(self, method: str, path: str, query=None, body=None, headers=None):
        self.calls.append({
            "method": method, "path": path,
            "query": query, "body": body,
        })
        if path == "/v1/aspirations/query":
            return self.responses.get("query", "[]")
        if path == "/v1/team-state/read":
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
        return json.dumps(self._active_payload)

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
# 1: second shape — agent-source in-progress goals with NO claimed_by.
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

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])  # no claimed goals — exercise the no-claim path only
    fake.set_active_aspirations([{
        "id": "asp-001",
        "goals": [_no_claim_goal("g-001-02", last_modified.isoformat())],
    }])

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
    }])

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
    }])

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

    last_modified = (dt.datetime.now() - dt.timedelta(minutes=30)).replace(microsecond=0)
    fake.set_query_response([])
    fake.set_active_aspirations([{
        "id": "asp-001",
        "goals": [_no_claim_goal("g-001-02", last_modified.isoformat())],
    }])

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
# Digest-ordering invariant (1 / rb-1533)
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
