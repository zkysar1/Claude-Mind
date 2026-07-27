#!/usr/bin/env python3
"""test_precheck_compact_staleness.py — precheck-eval.py _compact_staleness contract
(g-115-3116).

The defect this pins: `_load_compact()` reads
<agent>/session/aspirations-compact.json and NOTHING in precheck-eval refreshed
it or checked its age. All 8 detectors take that snapshot as a parameter and
none re-reads live state, so a stale compact made every detector return a
CONFIDENTLY WRONG answer whose summary is indistinguishable from a healthy one.
The refresh lives in a separate, LLM-invoked step (aspirations-precheck Phase
0.5's `load-aspirations-compact.sh`) — an LLM-gated step drifts, and when it is
skipped the detectors do not fail, they lie quietly.

Measured on the originating incident (alpha, 2026-07-25): a 101.4-minute-stale
compact missing 6 same-session goals changed the ANSWER of 2 of 8 detectors
(pipeline-depth 148 -> 165 executable; consolidation avg 0.87 -> 0.86) at the
same instant, differing only by compact freshness.

Pinned here:
  - fresh compact                     -> checked, not stale, no flag
  - world source newer                -> stale, names the world jsonl
  - agent source newer                -> stale, names the agent jsonl
  - EQUAL mtimes                       -> NOT stale (strict-newer parity with
                                          load-aspirations-compact.sh's `-nt`)
  - missing compact / missing sources -> checked=False, never a false "stale"
  - explicit path argument            -> judges THAT path, not the AGENT_DIR default
  - unresolvable roots                -> returns a dict, never raises

The strict-newer case is the load-bearing one: the staleness DEFINITION is
deliberately not invented in precheck-eval, it mirrors the predicate
load-aspirations-compact.sh already uses to decide whether to regenerate
(`[ "$WORLD_JSONL" -nt "$COMPACT" ]`). One definition of "stale" means every
reported staleness is clearable by the documented action.

AGENT_DIR / WORLD_DIR are module globals imported from _paths; the tests
monkeypatch them to a tmp dir so the probe targets a controlled layout rather
than the live agent.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location("precheck_eval", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

# Fixed epoch times so no test depends on wall-clock ordering or fs timestamp
# granularity (a real flake source when two writes land in the same tick).
T_BASE = 1_700_000_000
T_OLDER = T_BASE - 600      # 10 min before the compact
T_NEWER = T_BASE + 600      # 10 min after the compact


def _seed(tmp_path, compact_t=T_BASE, world_t=T_OLDER, agent_t=T_OLDER,
          make_compact=True, make_world=True, make_agent=True):
    """Build agent-dir + world-dir with explicitly-stamped mtimes.

    Returns (agent_dir, world_dir, compact_path).
    """
    agent_dir = tmp_path / "agents" / "testagent"
    world_dir = tmp_path / "world"
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    world_dir.mkdir(parents=True, exist_ok=True)

    compact = agent_dir / "session" / "aspirations-compact.json"
    if make_compact:
        compact.write_text("[]", encoding="utf-8")
        os.utime(compact, (compact_t, compact_t))
    if make_world:
        w = world_dir / "aspirations.jsonl"
        w.write_text("", encoding="utf-8")
        os.utime(w, (world_t, world_t))
    if make_agent:
        a = agent_dir / "aspirations.jsonl"
        a.write_text("", encoding="utf-8")
        os.utime(a, (agent_t, agent_t))
    return agent_dir, world_dir, compact


@pytest.fixture
def bind(monkeypatch):
    """Point the module globals at a tmp layout."""
    def _bind(agent_dir, world_dir):
        monkeypatch.setattr(pe, "AGENT_DIR", agent_dir)
        monkeypatch.setattr(pe, "WORLD_DIR", str(world_dir))
    return _bind


def test_fresh_compact_is_not_stale(tmp_path, bind):
    """Compact newer than both sources — the healthy case, no false positive."""
    agent_dir, world_dir, _ = _seed(tmp_path)
    bind(agent_dir, world_dir)

    info = pe._compact_staleness()

    assert info["checked"] is True
    assert info["stale"] is False
    assert "newer_source" not in info


def test_world_source_newer_is_stale_and_named(tmp_path, bind):
    """The originating incident's shape: world queue moved on, compact did not."""
    agent_dir, world_dir, _ = _seed(tmp_path, world_t=T_NEWER)
    bind(agent_dir, world_dir)

    info = pe._compact_staleness()

    assert info["stale"] is True
    # Naming the specific newer source is what makes the report actionable —
    # a bare "stale" leaves the reader guessing which queue moved.
    assert info["newer_source"].endswith("aspirations.jsonl")
    assert "world" in info["newer_source"]
    assert info["source_ahead_minutes"] == pytest.approx(10.0, abs=0.1)
    # BOTH queues are literally named `aspirations.jsonl`, so a basename can
    # never disambiguate them. The explicit kind is what the human-facing
    # summary line uses; without it the report says "older than
    # aspirations.jsonl" for either source and conveys nothing.
    assert info["newer_source_kind"] == "world"


def test_agent_source_newer_is_stale_and_named(tmp_path, bind):
    """The agent-local queue is the second source; it must not be ignored."""
    agent_dir, world_dir, _ = _seed(tmp_path, agent_t=T_NEWER)
    bind(agent_dir, world_dir)

    info = pe._compact_staleness()

    assert info["stale"] is True
    assert "agents" in info["newer_source"]
    assert info["newer_source_kind"] == "agent"


def test_newest_source_wins_when_both_are_newer(tmp_path, bind):
    """Two stale sources — report the furthest-ahead one, not whichever was scanned first."""
    agent_dir, world_dir, _ = _seed(tmp_path, world_t=T_NEWER, agent_t=T_NEWER + 600)
    bind(agent_dir, world_dir)

    info = pe._compact_staleness()

    assert info["stale"] is True
    assert "agents" in info["newer_source"]
    assert info["source_ahead_minutes"] == pytest.approx(20.0, abs=0.1)


def test_equal_mtimes_are_not_stale_parity_with_refresher(tmp_path, bind):
    """SSOT parity: load-aspirations-compact.sh uses `-nt` (STRICTLY newer), so a
    source stamped identically to the compact does NOT trigger regeneration.
    Reporting it stale here would be unclearable — the prescribed action
    (re-run the refresher) would not change anything, and the flag would
    re-fire forever."""
    agent_dir, world_dir, _ = _seed(tmp_path, compact_t=T_BASE,
                                    world_t=T_BASE, agent_t=T_BASE)
    bind(agent_dir, world_dir)

    info = pe._compact_staleness()

    assert info["checked"] is True
    assert info["stale"] is False


def test_missing_compact_reports_unchecked_not_stale(tmp_path, bind):
    """No compact at all is already loud (main() exits early with its own
    message). The probe must not manufacture a second, different complaint."""
    agent_dir, world_dir, _ = _seed(tmp_path, make_compact=False)
    bind(agent_dir, world_dir)

    info = pe._compact_staleness()

    assert info["checked"] is False
    assert info["stale"] is False


def test_no_sources_reports_unchecked(tmp_path, bind):
    """With nothing to compare against, 'not stale' would be an unfounded
    positive claim — report unchecked instead (verify-before-assuming)."""
    agent_dir, world_dir, _ = _seed(tmp_path, make_world=False, make_agent=False)
    bind(agent_dir, world_dir)

    info = pe._compact_staleness()

    assert info["checked"] is False
    assert info["stale"] is False


def test_explicit_path_argument_is_judged_not_the_default(tmp_path, bind):
    """--compact-path callers supply their own snapshot; staleness must be
    measured against THAT file, not the AGENT_DIR default."""
    agent_dir, world_dir, _ = _seed(tmp_path, compact_t=T_OLDER - 600, world_t=T_OLDER)
    bind(agent_dir, world_dir)

    # Default compact is older than the world source -> stale.
    assert pe._compact_staleness()["stale"] is True

    # An explicitly-passed FRESH copy is not.
    explicit = tmp_path / "explicit-compact.json"
    explicit.write_text("[]", encoding="utf-8")
    os.utime(explicit, (T_NEWER, T_NEWER))
    assert pe._compact_staleness(str(explicit))["stale"] is False


def test_unresolvable_roots_return_dict_never_raise(monkeypatch):
    """A staleness probe must not be able to break the detectors it reports on.
    Unbound AGENT_DIR / WORLD_DIR is a real state (agent_unset fallbacks)."""
    monkeypatch.setattr(pe, "AGENT_DIR", None)
    monkeypatch.setattr(pe, "WORLD_DIR", None)

    info = pe._compact_staleness()

    assert isinstance(info, dict)
    assert info["checked"] is False
    assert info["stale"] is False


def test_probe_error_reports_unchecked_not_verified_fresh(tmp_path, bind, monkeypatch):
    """A probe that BLEW UP does not know the answer. It must not let the
    default `stale: False` read as a verified-fresh verdict — an unverified
    negative is precisely the failure class this whole fix removes
    (verify-before-assuming: one signal, and a broken one, is zero signals)."""
    agent_dir, world_dir, _ = _seed(tmp_path, world_t=T_NEWER)
    bind(agent_dir, world_dir)

    # Sanity: without the fault this input IS detectably stale.
    assert pe._compact_staleness()["stale"] is True

    def _boom(*a, **kw):
        raise RuntimeError("probe fault")

    monkeypatch.setattr(pe.time, "strftime", _boom)

    info = pe._compact_staleness()

    assert info["checked"] is False          # not "fresh" — "unknown"
    assert "probe fault" in info["error"]


def test_refresh_delegates_to_the_canonical_builder(monkeypatch):
    """The rebuild must go through load-aspirations-compact.sh — the ONE thing
    that knows how to build a compact. Rebuilding inline would fork a second
    copy of the build logic that drifts from the real one."""
    seen = {}

    def _fake(args, input_text=None, timeout=30):
        seen["args"] = args
        seen["timeout"] = timeout
        return ("", "", 0)

    monkeypatch.setattr(pe, "_run_script", _fake)

    info = pe._refresh_compact()

    assert seen["args"] == ["load-aspirations-compact.sh"]
    assert info == {"attempted": True, "rc": 0}


def test_refresh_fail_opens_on_spawn_error(monkeypatch):
    """A refresh that times out or fails to spawn must NOT raise. The caller
    proceeds with whatever compact is on disk and the post-refresh staleness
    probe flags it loudly — degrading to visible-stale, never to a stall."""
    def _boom(*a, **kw):
        raise TimeoutError("builder hung")

    monkeypatch.setattr(pe, "_run_script", _boom)

    info = pe._refresh_compact()

    assert info["attempted"] is True
    assert info["rc"] is None
    assert "builder hung" in info["error"]


def test_refresh_captures_stderr_on_nonzero_exit(monkeypatch):
    """A builder that ran but FAILED must surface why — otherwise the
    surviving staleness flag names a symptom with no cause attached."""
    monkeypatch.setattr(
        pe, "_run_script",
        lambda args, input_text=None, timeout=30: ("", "daemon unreachable", 3))

    info = pe._refresh_compact()

    assert info["rc"] == 3
    assert "daemon unreachable" in info["stderr"]


def test_age_minutes_present_whenever_checked(tmp_path, bind):
    """The age is the number a reader acts on — it must accompany every checked
    verdict, stale or not."""
    agent_dir, world_dir, _ = _seed(tmp_path)
    bind(agent_dir, world_dir)

    info = pe._compact_staleness()

    assert info["checked"] is True
    assert "age_minutes" in info
    assert "compact_mtime" in info


def test_integration_detector_sees_post_mutation_write_with_no_manual_refresh(
        tmp_path, bind, monkeypatch, capsys):
    """THE REGRESSION TEST the goal asked for, automated.

    g-115-3116's verification: "mutate a goal, do NOT refresh, run precheck-eval,
    confirm it reports the pre-mutation state. That reproduction IS the
    regression test." Before the fix, the detector answered from the
    pre-mutation snapshot. After it, the script rebuilds its own input first.

    Exercises the whole wiring — staleness detect -> refresh -> reload -> detect
    -> report — rather than the pieces in isolation, because every piece passed
    on its own while the path between them was the defect. The builder is
    simulated (writing a fresh compact) so the test needs no daemon and cannot
    touch the live agent.
    """
    agent_dir, world_dir, compact = _seed(tmp_path, world_t=T_NEWER)
    bind(agent_dir, world_dir)

    # The compact on disk is the PRE-mutation snapshot: no goals at all.
    compact.write_text(json.dumps([
        {"id": "asp-115", "status": "active", "title": "t", "goals": []}
    ]), encoding="utf-8")
    os.utime(compact, (T_BASE, T_BASE))

    # Simulate the canonical builder: it rebuilds the compact from the live
    # store, which by now contains the mutation.
    calls = []

    def _fake_builder(args, input_text=None, timeout=30):
        calls.append(args)
        compact.write_text(json.dumps([
            {"id": "asp-115", "status": "active", "title": "t",
             "goals": [{"id": "g-115-3116", "status": "completed",
                        "title": "the mutation", "participants": ["agent"]}]}
        ]), encoding="utf-8")
        os.utime(compact, (T_NEWER + 60, T_NEWER + 60))  # now newer than sources
        return ("", "", 0)

    monkeypatch.setattr(pe, "_run_script", _fake_builder)
    monkeypatch.setattr(sys, "argv", ["precheck-eval.py", "user-goals"])

    with pytest.raises(SystemExit):
        pe.main()

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    # The script rebuilt its own input — no separate LLM-invoked step involved.
    assert calls == [["load-aspirations-compact.sh"]]
    assert out["compact_refresh"]["rc"] == 0
    # And it is not reporting from a stale snapshot any more.
    assert out["compact_freshness"]["stale"] is False
    assert "compact_stale" not in out.get("flags", [])
    assert "COMPACT STALE" not in out["summary"]


def test_integration_failed_rebuild_degrades_to_visible_stale_not_a_stall(
        tmp_path, bind, monkeypatch, capsys):
    """When the rebuild FAILS the run must still produce its verdict — but say
    loudly that the verdict rests on stale data. Fail-open into a flagged
    answer, never into a stall (the failure mode a hard refuse would have had)."""
    agent_dir, world_dir, compact = _seed(tmp_path, world_t=T_NEWER)
    bind(agent_dir, world_dir)
    compact.write_text(json.dumps([
        {"id": "asp-115", "status": "active", "title": "t", "goals": []}
    ]), encoding="utf-8")
    os.utime(compact, (T_BASE, T_BASE))

    def _broken_builder(args, input_text=None, timeout=30):
        return ("", "daemon unreachable", 3)

    monkeypatch.setattr(pe, "_run_script", _broken_builder)
    monkeypatch.setattr(sys, "argv", ["precheck-eval.py", "user-goals"])

    with pytest.raises(SystemExit) as exc:
        pe.main()

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert out["compact_refresh"]["rc"] == 3            # rebuild attempted and failed
    assert out["compact_freshness"]["stale"] is True    # still stale, honestly reported
    assert "compact_stale" in out["flags"]
    assert "auto-rebuild FAILED" in out["summary"]      # names WHY it is still stale
    assert exc.value.code == 1                          # actionable, not silent
    # The verdict itself still shipped — a failed rebuild must not swallow it.
    assert out["subcommand"] == "user-goals"


def test_surviving_staleness_after_a_SUCCESSFUL_rebuild_is_not_blamed_on_the_builder(
        tmp_path, bind, monkeypatch, capsys):
    """fresh-eyes F-002. In a multi-agent fleet the WORLD queue is shared, so a
    partner filing a goal between the rebuild and the post-check leaves the
    compact legitimately stale again through no fault of the builder. Reporting
    that race as 'auto-rebuild FAILED' sends the reader to debug a builder that
    is working fine. rc must drive the attribution, not merely 'a refresh was
    attempted'."""
    agent_dir, world_dir, compact = _seed(tmp_path, world_t=T_NEWER)
    bind(agent_dir, world_dir)
    compact.write_text(json.dumps([
        {"id": "asp-115", "status": "active", "title": "t", "goals": []}
    ]), encoding="utf-8")
    os.utime(compact, (T_BASE, T_BASE))

    def _builder_then_concurrent_write(args, input_text=None, timeout=30):
        # The rebuild SUCCEEDS...
        os.utime(compact, (T_NEWER + 60, T_NEWER + 60))
        # ...and then a partner writes the shared world queue.
        os.utime(world_dir / "aspirations.jsonl", (T_NEWER + 120, T_NEWER + 120))
        return ("", "", 0)

    monkeypatch.setattr(pe, "_run_script", _builder_then_concurrent_write)
    monkeypatch.setattr(sys, "argv", ["precheck-eval.py", "user-goals"])

    with pytest.raises(SystemExit):
        pe.main()

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert out["compact_refresh"]["rc"] == 0            # the builder did its job
    assert out["compact_freshness"]["stale"] is True    # yet the snapshot IS behind
    assert "compact_stale" in out["flags"]              # still flagged — correctly
    assert "FAILED" not in out["summary"]               # but NOT blamed on the builder
    assert "concurrent write" in out["summary"]


def test_stale_branch_survives_a_partial_freshness_dict(tmp_path, bind, monkeypatch, capsys):
    """fresh-eyes F-001. The probe documents that it can never break the
    detectors it reports on — but that promise only holds if its CONSUMER
    honours it. main()'s stale branch used to bracket-index three keys the probe
    sets together in one branch, so a partial dict raised KeyError AFTER all 8
    detectors had already run, discarding their entire output."""
    agent_dir, world_dir, _ = _seed(tmp_path)
    bind(agent_dir, world_dir)

    # A probe result that is stale but missing its companion keys.
    monkeypatch.setattr(pe, "_compact_staleness",
                        lambda *a, **kw: {"checked": True, "stale": True})
    monkeypatch.setattr(pe, "_refresh_compact",
                        lambda *a, **kw: {"attempted": True, "rc": 0})
    monkeypatch.setattr(sys, "argv", ["precheck-eval.py", "user-goals"])

    with pytest.raises(SystemExit):
        pe.main()          # must be SystemExit, NOT KeyError

    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    # The detectors' work survived and still shipped.
    assert out["subcommand"] == "user-goals"
    assert "compact_stale" in out["flags"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
