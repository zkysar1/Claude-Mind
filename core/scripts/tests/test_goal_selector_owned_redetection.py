"""test_goal_selector_owned_redetection.py — .

Guards the OWNED-REDETECTION floor: when a detector re-fires on a finding whose
owner goal is already OPEN and UNCLAIMED, the stated disposal is to append ONE
short `[recheck:<agent> <date>]` marker line (not a reading paragraph), and the
goal-selector converts that repeat-detection into a SELECTION signal — once
>=2 DISTINCT agents carry a live marker within 7 days, the floor hoists the
owner goal to the top slot and emits a banner + sidecar record.

Two SSOTs are pinned here:

  1. `recheck_marker` — the PURE predicate. `parse_rechecks` reads the LATEST
     marker date per agent; `lift_for` decides whether enough DISTINCT agents
     are inside the window. Pinned against marker parsing, distinct-agent
     counting, and the 7-day window boundary.

  2. `goal-selector.apply_owned_redetection_floor` + banner + emission — the
     FLOOR. Pinned for hoist + yield-to-prior-hoist + no-op/decline paths, the
     "never rescore" (byte-identical non-floor pick) invariant, the banner
     shape, the `OWNED_REDETECTION_CONFIG` defaults, the `score_goal`
     `progress_note` projection, and the EMISSION STRIP (progress_note is an
     in-process transport that must never reach the emitted rows).

Module-load pattern mirrors test_goal_selector_strategic_focus_floor.py:
capture/restore MIND_AGENT around the module import so agent-scoped path
resolution is deterministic.

Run: STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_goal_selector_owned_redetection.py
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

gs = importlib.import_module("goal-selector")
recheck_marker = importlib.import_module("recheck_marker")
SELECTOR_SRC = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


# ── helpers ─────────────────────────────────────────────────────────────────

def _note(*pairs):
    """Build a progress_note from (agent, date) pairs; each pair is one marker
    line in the documented `[recheck:<agent> <YYYY-MM-DD>]` shape."""
    return "\n".join(
        f"[recheck:{agent} {d.isoformat()}] census re-fired: no delta"
        for agent, d in pairs
    )


def _row(gid, score, note=None, asp="asp-115", **extra):
    """A scored candidate in the shape cmd_select emits (mirrors
    test_goal_selector_merged_queue_ordering.row), carrying `progress_note` so
    the floor has its transport field to read."""
    r = {
        "goal_id": gid, "aspiration_id": asp, "score": score, "source": "world",
        "recurring": False, "recurring_overdue_ratio": 0.0,
        "recurring_interval_hours": 24.0, "raw": {}, "breakdown": {},
    }
    if note is not None:
        r["progress_note"] = note
    r.update(extra)
    return r


def _cfg(**over):
    base = {"enabled": True, "min_agents": 2, "window_days": 7}
    base.update(over)
    return base


TODAY = date.today()
D0 = TODAY
D1 = TODAY - timedelta(days=1)
D6 = TODAY - timedelta(days=6)
D7 = TODAY - timedelta(days=7)
D8 = TODAY - timedelta(days=8)


# ═══════════════════════════════════════════════════════════════════════════
# 1. recheck_marker — the pure predicate (marker parsing, counting, window)
# ═══════════════════════════════════════════════════════════════════════════

def test_parse_rechecks_reads_latest_date_per_agent():
    """The store is append-only, so a re-detecting agent's markers accumulate;
    only the LATEST per agent carries information. A stale then-fresh pair must
    resolve to the fresh date."""
    note = _note(("alpha", D1), ("alpha", D0), ("bravo", D1))
    latest = recheck_marker.parse_rechecks(note)
    assert latest == {
        "alpha": D0,  # the newer of alpha's two lines wins
        "bravo": D1,
    }


def test_parse_rechecks_lowercases_agent_names():
    """Agent names are a lowercase fleet vocabulary; a marker written with an
    uppercase name must still count as the same agent (distinctness is the
    whole signal, so case drift would silently under-count)."""
    note = "[recheck:Alpha %s] x\n[recheck:ALPHA %s] y" % (D0.isoformat(), D0.isoformat())
    latest = recheck_marker.parse_rechecks(note)
    assert list(latest) == ["alpha"]
    assert latest["alpha"] == D0


def test_parse_rechecks_skips_malformed_dates_without_raising():
    """A note is prose written by many agents; one typo must not kill the lift
    for the whole fleet (fail-open). A bad date is skipped, the good one kept."""
    note = "[recheck:alpha 2026-99-99] bad\n[recheck:bravo %s] good" % D0.isoformat()
    latest = recheck_marker.parse_rechecks(note)
    assert latest == {"bravo": D0}


def test_parse_rechecks_ignores_idempotency_sentinels():
    """goal-field-append.sh wraps the append in `[appended:recheck-<agent>-<date>]`
    sentinels. Those must NOT match as markers — the regex is anchored on the
    literal `[recheck:` prefix, so the bare-token sentinel is inert."""
    note = ("[appended:recheck-alpha-20261002]\n"
            "[recheck:alpha %s] census re-fired: no delta" % D0.isoformat())
    latest = recheck_marker.parse_rechecks(note)
    assert latest == {"alpha": D0}


def test_parse_rechecks_empty_and_none_note():
    assert recheck_marker.parse_rechecks("") == {}
    assert recheck_marker.parse_rechecks(None) == {}


def test_lift_for_two_distinct_agents_in_window_lifts():
    st = recheck_marker.lift_for(_note(("alpha", D0), ("bravo", D1)), now=TODAY)
    assert st["lift"] is True
    assert st["agents_in_window"] == ["alpha", "bravo"]


def test_lift_for_single_agent_does_not_lift():
    """Distinctness is the whole signal: one agent re-measuring the same number
    is the precheck's normal behavior, not the 'nothing is taking this' signal."""
    st = recheck_marker.lift_for(_note(("alpha", D0), ("alpha", D1)), now=TODAY)
    assert st["lift"] is False
    assert st["agents_in_window"] == ["alpha"]


def test_lift_for_same_agent_repeated_is_one_distinct():
    st = recheck_marker.lift_for(_note(("alpha", D0), ("alpha", D0)), now=TODAY)
    assert st["agents_in_window"] == ["alpha"]
    assert st["lift"] is False


def test_lift_for_window_boundary_six_days_in_seven_days_out():
    """`0 <= (now-d).days < window_days`: 6 days ago is inside, 7 days ago is
    exactly at the boundary and OUT. This is the whole 7-day contract."""
    # 6 days ago -> in window (with a fresh second agent it lifts).
    st_in = recheck_marker.lift_for(
        _note(("alpha", D6), ("bravo", D0)), now=TODAY)
    assert st_in["agents_in_window"] == ["alpha", "bravo"]
    assert st_in["lift"] is True
    # 7 days ago -> out of window, so only the fresh agent counts -> no lift.
    st_out = recheck_marker.lift_for(
        _note(("alpha", D7), ("bravo", D0)), now=TODAY)
    assert st_out["agents_in_window"] == ["bravo"]
    assert st_out["lift"] is False


def test_lift_for_stale_marker_does_not_lift():
    st = recheck_marker.lift_for(_note(("alpha", D8), ("bravo", D0)), now=TODAY)
    assert st["agents_in_window"] == ["bravo"]
    assert st["lift"] is False


def test_lift_for_future_marker_does_not_count():
    """A marker dated in the future (clock skew / typo) is not a live re-detect:
    (now-d).days is negative, so it is excluded by the `0 <=` bound."""
    st = recheck_marker.lift_for(
        _note(("alpha", TODAY + timedelta(days=1)), ("bravo", D0)), now=TODAY)
    assert st["agents_in_window"] == ["bravo"]
    assert st["lift"] is False


def test_lift_for_min_agents_and_window_are_configurable():
    note = _note(("alpha", D0), ("bravo", D1))
    # min_agents=1 -> a single agent lifts.
    assert recheck_marker.lift_for(note, now=TODAY, min_agents=1)["lift"] is True
    # min_agents=3 -> two agents are not enough.
    assert recheck_marker.lift_for(note, now=TODAY, min_agents=3)["lift"] is False
    # window_days=1 -> only a today-marker counts; bravo's yesterday is out.
    st = recheck_marker.lift_for(note, now=TODAY, window_days=1)
    assert st["agents_in_window"] == ["alpha"]
    assert st["lift"] is False


def test_lift_for_returns_sorted_agents_and_iso_dates():
    st = recheck_marker.lift_for(_note(("zeta", D0), ("alpha", D1)), now=TODAY)
    assert st["agents_in_window"] == ["alpha", "zeta"]  # sorted
    assert st["latest"] == {"alpha": D1.isoformat(), "zeta": D0.isoformat()}


# ═══════════════════════════════════════════════════════════════════════════
# 2. The floor — hoist, yield, decline paths, no-rescore invariant
# ═══════════════════════════════════════════════════════════════════════════

def test_floor_hoists_owner_with_two_live_distinct_agents(monkeypatch):
    """The whole point: an owner ranked LAST (low score) takes the top slot once
    two distinct agents re-detect it."""
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg())
    owner = _row("g-owner", 4.0, note=_note(("alpha", D0), ("bravo", D1)))
    scored = [_row("g-115-1", 11.6), _row("g-115-2", 9.4), owner]
    picked, status = gs.apply_owned_redetection_floor(scored)
    assert picked is not None and picked["goal_id"] == "g-owner"
    assert scored[0]["goal_id"] == "g-owner"          # hoisted to index 0
    assert scored[0].get("owned_redetection_pick") is True
    assert status["picked"] == "g-owner"
    assert status["reason"] == "fired"
    assert status["eligible"] == 1


def test_floor_does_not_lift_single_agent(monkeypatch):
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg())
    scored = [
        _row("g-115-1", 11.6),
        _row("g-owner", 4.0, note=_note(("alpha", D0), ("alpha", D1))),
    ]
    picked, status = gs.apply_owned_redetection_floor(scored)
    assert picked is None
    assert status["reason"] == "no-eligible"
    assert scored[0]["goal_id"] == "g-115-1"          # top undisturbed


def test_floor_does_not_lift_when_only_one_agent_is_in_window(monkeypatch):
    """A stale second agent does not count: distinctness is measured over the
    live window, not over the whole history."""
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg())
    scored = [
        _row("g-115-1", 11.6),
        _row("g-owner", 4.0, note=_note(("alpha", D8), ("bravo", D0))),
    ]
    picked, status = gs.apply_owned_redetection_floor(scored)
    assert picked is None
    assert status["reason"] == "no-eligible"


def test_floor_yields_to_prior_hoist(monkeypatch):
    """A starving recurring goal (drain lane) or a standing user directive
    (strategic-focus floor) outrank this policy: prior_hoist_fired=True makes
    the floor a no-op that leaves the top slot alone."""
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg())
    scored = [
        _row("g-lane", 9.0, recurring=True),
        _row("g-owner", 4.0, note=_note(("alpha", D0), ("bravo", D1))),
    ]
    picked, status = gs.apply_owned_redetection_floor(
        scored, prior_hoist_fired=True)
    assert picked is None
    assert status["yielded_to_prior_hoist"] is True
    assert status["reason"] == "yielded"
    assert scored[0]["goal_id"] == "g-lane"           # prior hoist undisturbed


def test_floor_disabled(monkeypatch):
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg(enabled=False))
    scored = [_row("g-owner", 4.0, note=_note(("alpha", D0), ("bravo", D1)))]
    picked, status = gs.apply_owned_redetection_floor(scored)
    assert picked is None
    assert status["reason"] == "disabled"


def test_floor_empty_pool(monkeypatch):
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg())
    picked, status = gs.apply_owned_redetection_floor([])
    assert picked is None
    assert status["reason"] == "no-candidates"


def test_floor_no_eligible_when_no_note(monkeypatch):
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg())
    scored = [_row("g-115-1", 11.6), _row("g-115-2", 9.4)]
    picked, status = gs.apply_owned_redetection_floor(scored)
    assert picked is None
    assert status["reason"] == "no-eligible"
    assert status["eligible"] == 0


def test_floor_picks_most_distinct_agents_when_scores_differ(monkeypatch):
    """Strength = number of distinct re-detecting agents, NOT score: an owner
    re-detected by 3 agents lifts over one re-detected by 2, even at a lower
    score. That is the 'nothing is taking this' signal ranked by strength."""
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg())
    three = _row("g-3agents", 4.0,
                 note=_note(("alpha", D0), ("bravo", D1), ("echo", D1)))
    two = _row("g-2agents", 10.0, note=_note(("zeta", D0), ("foxtrot", D1)))
    scored = [_row("g-115-1", 11.6), three, two]
    picked, status = gs.apply_owned_redetection_floor(scored)
    assert picked["goal_id"] == "g-3agents"
    assert scored[0]["goal_id"] == "g-3agents"


def test_floor_tiebreak_on_score_when_distinct_count_equal(monkeypatch):
    """Two owners each re-detected by 2 agents: the higher-scoring one wins, so
    an all-equal set is deterministic (max() keeps the first maximal element in
    candidate order, and score is the tiebreaker)."""
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg())
    low = _row("g-low", 5.0, note=_note(("alpha", D0), ("bravo", D1)))
    high = _row("g-high", 8.0, note=_note(("echo", D0), ("foxtrot", D1)))
    scored = [_row("g-115-1", 11.6), low, high]
    picked, _ = gs.apply_owned_redetection_floor(scored)
    assert picked["goal_id"] == "g-high"
    assert scored[0]["goal_id"] == "g-high"


def test_floor_never_rewrites_scores(monkeypatch):
    """Like apply_drain_lane, the floor REORDERS and never rescores — so every
    non-floor row stays byte-identical (score + breakdown) to pre-floor."""
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg())
    scored = [
        _row("g-115-1", 11.6),
        _row("g-owner", 4.0, note=_note(("alpha", D0), ("bravo", D1))),
    ]
    before = {r["goal_id"]: (r["score"], dict(r["breakdown"])) for r in scored}
    gs.apply_owned_redetection_floor(scored)
    after = {r["goal_id"]: (r["score"], dict(r["breakdown"])) for r in scored}
    assert before == after


def test_floor_stamps_lift_telemetry_only_on_eligible_rows(monkeypatch):
    """The `owned_redetection_lift` dict is row telemetry read by the banner;
    it lands only on the ELIGIBLE rows, never on the rest of the pool."""
    monkeypatch.setattr(gs, "OWNED_REDETECTION_CONFIG", _cfg())
    scored = [
        _row("g-115-1", 11.6),
        _row("g-owner", 4.0, note=_note(("alpha", D0), ("bravo", D1))),
    ]
    gs.apply_owned_redetection_floor(scored)
    owner = next(r for r in scored if r["goal_id"] == "g-owner")
    assert owner["owned_redetection_lift"] == {
        "agents_in_window": ["alpha", "bravo"],
        "min_agents": 2,
        "window_days": 7,
    }
    other = next(r for r in scored if r["goal_id"] == "g-115-1")
    assert "owned_redetection_lift" not in other


def test_floor_claimed_owner_is_out_of_pool_by_construction():
    """Claim-gating lives in collect_candidates (a claimed goal never reaches
    the scored pool), so the floor's predicate needs no claim test. Pin the
    guard that makes this premise TRUE: collect_candidates excludes claimed
    goals, so 'in the pool' already means 'unclaimed'."""
    goal = {
        "id": "g-claimed", "title": "already claimed", "status": "pending",
        "participants": ["agent"], "priority": "MEDIUM",
        "claimed_by": "bravo", "claimed_by_sid": "sid-x",
    }
    asps = [{"id": "asp-test", "status": "active", "goals": [goal]}]
    cands = gs.collect_candidates(asps, source="world")
    assert [c["goal"]["id"] for c in cands] == [], \
        "a claimed goal must not be a candidate (the floor's unclaimed premise)"


def test_floor_runs_before_emission_strip_in_cmd_select_source():
    """STATIC ORDERING PIN (the call-site contract the unit tests cannot see):
    in cmd_select the owned-redetection floor must run (a) after the
    strategic-focus floor and (b) BEFORE the progress_note strip — a strip that
    landed first would feed the floor an empty transport (guard-1362: a field
    the projection omits is a silent zero) and the floor would be inert while
    still LOOKING wired."""
    floor_at = SELECTOR_SRC.index("apply_owned_redetection_floor(")
    strip_at = SELECTOR_SRC.index('s.pop("progress_note", None)')
    sf_at = SELECTOR_SRC.index("apply_strategic_focus_floor(")
    assert sf_at < floor_at, "floor must run after the strategic-focus floor"
    assert floor_at < strip_at, "floor must read progress_note before the strip"


def test_floor_wired_before_verdict_write_in_cmd_select_source():
    """STATIC ORDERING PIN (guard-2331): the floor must run BEFORE
    write_scorer_verdict, so the sidecar records the hoist as the sanctioned
    top and the claim chokepoint accepts it without a deviation code. The
    call-site occurrence (not the def) is what matters; `cmd_select` is the
    only caller of the floor, so the first call in source order is the one."""
    floor_call = SELECTOR_SRC.index("apply_owned_redetection_floor(")
    verdict_call = SELECTOR_SRC.index("write_scorer_verdict(scored, AGENT_DIR)")
    assert floor_call < verdict_call, (
        "the floor must record its pick on the verdict sidecar before the "
        "claim gate can read it (guard-2331)")


# ═══════════════════════════════════════════════════════════════════════════
# 3. Banner — stderr-only + list-of-text shape for the sidecar
# ═══════════════════════════════════════════════════════════════════════════

def test_banner_returns_one_text_and_prints_to_stderr():
    """The returned one-element list IS the stderr line (the call site records
    it on the verdict sidecar WITHOUT re-calling the emitter — a second call
    would print the banner twice)."""
    picked = _row("g-owner", 4.0)
    picked["owned_redetection_lift"] = {
        "agents_in_window": ["alpha", "bravo"], "min_agents": 2, "window_days": 7}
    buf = io.StringIO()
    with redirect_stderr(buf):
        texts = gs.emit_owned_redetection_banner(picked, {"reason": "fired"})
    out = buf.getvalue()
    assert len(texts) == 1
    # print() appends the newline to stderr; the RETURNED text is what the
    # call site records on the sidecar, so it is the stderr line minus '\n'.
    assert out == texts[0] + "\n"
    assert "OWNED-REDETECTION FLOOR" in out
    assert "g-115-11721" in out
    assert "g-owner" in out
    assert "2 distinct agents" in out
    assert "alpha" in out and "bravo" in out
    # It must not let a reader read the hoist as a scoring anomaly.
    assert "not a score" in out
    assert "selection hoist" in out


def test_banner_silent_when_floor_did_not_fire():
    """Deliberate silence (the drain-lane banner's own rule): a banner on every
    quiet iteration would train the reader to skip it."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        texts = gs.emit_owned_redetection_banner(None, {"reason": "no-eligible"})
    assert texts == []
    assert buf.getvalue() == ""


# ═══════════════════════════════════════════════════════════════════════════
# 4. Config — the defaults ARE the goal's verification threshold
# ═══════════════════════════════════════════════════════════════════════════

def test_default_config_is_the_goal_verification_threshold():
    """The fail-open defaults (a world whose aspirations.yaml carries no
    owned_redetection block) must run on exactly the goal's own threshold:
    >=2 agents / 7 days. A drift here is a silent no-op (floor never fires)
    or a silent over-fire (min_agents=1)."""
    assert gs._OWNED_REDETECTION_DEFAULTS == {
        "enabled": True, "min_agents": 2, "window_days": 7}
    # The module-level config (loaded from the live yaml at import) must agree
    # with the goal's threshold.
    assert gs.OWNED_REDETECTION_CONFIG["min_agents"] == 2
    assert gs.OWNED_REDETECTION_CONFIG["window_days"] == 7
    assert gs.OWNED_REDETECTION_CONFIG["enabled"] is True


def test_aspirations_yaml_declares_the_owned_redetection_block():
    """The yaml block is what the loader reads. The loader FAILS OPEN to the
    same defaults, so only a source-level pin catches a 'simplify' pass that
    deletes the block (the floor would keep working on defaults — invisible)."""
    import yaml
    p = CORE_SCRIPTS.parent / "config" / "aspirations.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert doc.get("owned_redetection") == {
        "enabled": True, "min_agents": 2, "window_days": 7}


def test_score_goal_projects_progress_note():
    """guard-1362 forward: a floor predicate that reads a field the projection
    omits is a silent zero. score_goal's return dict must carry progress_note
    onto every emitted row so the floor has its transport field. Source-level
    pin (the same shape test_goal_selector_weights_contract uses for the
    KNOWN_CRITERIA manifest)."""
    m = re.search(r"def score_goal\(cand.*?(?=\ndef )", SELECTOR_SRC, re.S)
    assert m, "score_goal body not found"
    assert '"progress_note": goal.get("progress_note")' in m.group(0)


# ═══════════════════════════════════════════════════════════════════════════
# 5. E2E — cmd_select: the floor hoists AND the emission strip keeps the
#    emitted schema byte-identical (no progress_note in stdout)
# ═══════════════════════════════════════════════════════════════════════════

def _goal(gid, note=None):
    g = {
        "id": gid, "title": "goal %s" % gid, "status": "pending",
        "participants": ["agent"], "category": "test", "priority": "MEDIUM",
    }
    if note is not None:
        g["progress_note"] = note
    return g


def _asps(goals):
    return [{"id": "asp-test", "status": "active", "goals": goals}]


def _run_select(monkeypatch, tmp_path, world_goals, scores):
    """Drive the REAL cmd_select with read_jsonl + scoring stubbed, in the same
    seam set as test_goal_selector_capability_filter._run_cmd_select, PLUS a
    tmp AGENT_DIR so the scorer-verdict sidecar writes land in tmp_path and a
    test can never clobber this session's real scorer-verdict.json.

    The score_goal stub carries progress_note onto the rows (as the real one
    does since g-115-11721) — that is the transport the floor reads, and
    stubbing it out would make the strip test below vacuous (no transport to
    strip). The other post-scoring passes (drain lane, SF floor, reducer-only
    floor, boosts) run UNSTUBBED and are structurally inert on these synthetic
    rows: no recurring rows (drain lane), no live team-state lane matches
    'asp-test' (SF floor), no executable_by_role rows (reducer-only floor),
    no pull_signal/created_at (boosts) — so the only sanctioned head
    perturbation that can fire is the floor under test.

    Returns (emitted, agent_dir)."""
    def _rj(path):
        if path == gs.WORLD_ASP_PATH:
            return _asps(world_goals)
        return []  # agent queue / pipeline / archive

    def _score(c, wm, resolved, sc, **kw):
        g = c["goal"]
        row = {
            "goal_id": g["id"], "aspiration_id": c["aspiration"]["id"],
            "source": c.get("source", "world"), "title": g.get("title", ""),
            "score": scores.get(g["id"], 1.0),
            "recurring": False, "recurring_overdue_ratio": 0.0,
            "recurring_interval_hours": 24.0, "raw": {}, "breakdown": {},
        }
        if g.get("progress_note") is not None:
            row["progress_note"] = g["progress_note"]
        return row

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    # _agent_is_resident() requires a local-paths.conf in AGENT_DIR for the
    # sidecar writers (scorer-verdict + banners) to run; without it both
    # no-op and the e2e assertions below would read files that were never
    # written (guard-1562: enumerate against live state, not assume).
    (agent_dir / "local-paths.conf").write_text("placeholder\n")
    monkeypatch.setattr(gs, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(gs, "read_jsonl", _rj)
    monkeypatch.setattr(gs, "read_wm", lambda: {"slots": {}})
    monkeypatch.setattr(gs, "load_recent_class_completions",
                        lambda window_size=20: [])
    monkeypatch.setattr(gs, "load_exploration_params", lambda: (0.0, 0.0))
    monkeypatch.setattr(gs, "score_goal", _score)
    monkeypatch.setattr(gs, "_record_strategy_application", lambda *a, **k: None)
    monkeypatch.setattr(gs, "_get_runner_capabilities", lambda: {"git-push"})

    buf = io.StringIO()
    with redirect_stdout(buf):
        gs.cmd_select(argparse.Namespace())
    return json.loads(buf.getvalue()), agent_dir


def test_select_hoists_owner_and_strips_progress_note_from_emission(
        monkeypatch, tmp_path):
    """THE LOAD-BEARING E2E. A low-scoring owner re-detected by two distinct
    agents (live markers) hoists to the top of the emitted list, and NO row in
    the emitted JSON carries progress_note — the transport is stripped before
    emission, so the loop's bare goal-selector.sh call (which emits the FULL
    list every iteration) cannot push the ~7.6 MB of note prose into
    LLM-visible stdout (guard-2518: the projection is a schema in its own
    right)."""
    note = _note(("alpha", D0), ("bravo", D1))
    world_goals = [_goal("g-test-hot"), _goal("g-test-owner", note=note)]
    emitted, _ = _run_select(
        monkeypatch, tmp_path, world_goals,
        scores={"g-test-hot": 9.0, "g-test-owner": 4.0})
    assert isinstance(emitted, list)
    assert [g["goal_id"] for g in emitted] == ["g-test-owner", "g-test-hot"], \
        "the floor must hoist the re-detected owner to the top"
    for row in emitted:
        assert "progress_note" not in row, \
            "progress_note is an in-process transport and must not be emitted"
    owner = emitted[0]
    assert owner.get("owned_redetection_pick") is True
    assert owner.get("owned_redetection_lift", {}).get("agents_in_window") \
        == ["alpha", "bravo"]


def test_select_verdict_sidecar_records_hoisted_top(monkeypatch, tmp_path):
    """guard-2331: write_scorer_verdict runs AFTER the floor, so the sidecar
    records the HOISTED owner as the sanctioned top and the claim chokepoint
    accepts it without a deviation code. Read back from the tmp AGENT_DIR."""
    note = _note(("alpha", D0), ("bravo", D1))
    world_goals = [_goal("g-test-hot"), _goal("g-test-owner", note=note)]
    emitted, agent_dir = _run_select(
        monkeypatch, tmp_path, world_goals,
        scores={"g-test-hot": 9.0, "g-test-owner": 4.0})
    verdict_path = agent_dir / "session" / "scorer-verdict.json"
    assert verdict_path.exists(), "the sidecar writer must have run (tmp AGENT_DIR)"
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert verdict["top_goal_id"] == "g-test-owner"
    top5 = [g["goal_id"] for g in verdict.get("top_5", [])]
    assert top5[0] == "g-test-owner" and "g-test-hot" in top5


def test_select_banner_recorded_on_sidecar_when_floor_fires(monkeypatch, tmp_path):
    """The banner's one-element list is recorded on the verdict sidecar's
    banners dict under 'owned_redetection' — without it a sidecar reader cannot
    distinguish 'the floor hoisted this goal' from 'the scorer ranked it' (the
    exact question the strategic_focus key answers for its floor, g-115-4296).
    stdout is unaffected: the banner is stderr-only."""
    note = _note(("alpha", D0), ("bravo", D1))
    world_goals = [_goal("g-test-hot"), _goal("g-test-owner", note=note)]
    buf = io.StringIO()
    errbuf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(errbuf):
        emitted, agent_dir = _run_select(
            monkeypatch, tmp_path, world_goals,
            scores={"g-test-hot": 9.0, "g-test-owner": 4.0})
    # stdout is pure JSON (the banner must not leak into the parsed stream).
    assert "OWNED-REDETECTION FLOOR" not in buf.getvalue()
    # stderr carried it once (the call site captures, never re-calls).
    assert errbuf.getvalue().count("OWNED-REDETECTION FLOOR") == 1
    # The banners are a SECOND, ADDITIVE write onto the SAME
    # scorer-verdict.json (write_scorer_verdict_banners): it must NOT have
    # clobbered the verdict half (top_goal_id survives the second write) and
    # the banners key must carry the floor's one-element list.
    verdict_path = agent_dir / "session" / "scorer-verdict.json"
    assert verdict_path.exists(), "the sidecar writer must have run (tmp AGENT_DIR)"
    data = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert data["top_goal_id"] == "g-test-owner", (
        "the second (banners) write must be additive, not a clobber")
    od = data.get("banners", {}).get("owned_redetection", [])
    assert len(od) == 1
    assert "OWNED-REDETECTION FLOOR" in od[0]
    assert "g-test-owner" in od[0]


def test_select_byte_identical_emission_when_floor_does_not_fire(
        monkeypatch, tmp_path):
    """No live markers -> the floor is a no-op and the emitted list is the
    plain score-descending order with no progress_note anywhere: the emitted
    schema is byte-identical to pre-g-115-11721 for the common case."""
    world_goals = [
        _goal("g-test-hot"),
        _goal("g-test-cold", note="no markers here, just prose"),
    ]
    emitted, _ = _run_select(
        monkeypatch, tmp_path, world_goals,
        scores={"g-test-hot": 9.0, "g-test-cold": 4.0})
    assert [g["goal_id"] for g in emitted] == ["g-test-hot", "g-test-cold"]
    for row in emitted:
        assert "progress_note" not in row
        assert "owned_redetection_pick" not in row
        assert "owned_redetection_lift" not in row


def test_select_progress_note_strip_happens_even_without_markers(monkeypatch, tmp_path):
    """The strip is unconditional (it guards the SCHEMA, not the floor): a note
    that carries no recheck markers must also be stripped, so a future
    note-reading pass added BELOW the strip would be a silent zero (guard-2518).
    Positive control for the strip existing independently of the lift."""
    world_goals = [
        _goal("g-test-hot", note="[appended:bravo-20260930] long prose reading..."),
    ]
    emitted, _ = _run_select(monkeypatch, tmp_path, world_goals,
                             scores={"g-test-hot": 9.0})
    assert emitted[0]["goal_id"] == "g-test-hot"
    assert "progress_note" not in emitted[0]
