"""B3 () — the §4 starvation fail-safe in precheck-eval cmd_pipeline_depth.

world/conventions/goal-intake-management.md §4: with candidate_tier ON, an agent
below pipeline_low_water_mark that has groomable candidates gets the OLDEST of
them promoted to pending deterministically (no LLM, I4) instead of generating
new work. Generation (thin_pipeline) fires only when no groomable candidate
exists. With the flag OFF the subcommand's output is byte-identical to the
legacy path.

Hermetic: _run_script is replaced by a fake, so neither the daemon nor any
store is touched, and log_script_decision is captured, never written. The real
candidate -> pending write path (§2 table + §5 ledger row) is pinned by
test_candidate_transition_gate.py; here we pin that every promotion goes
THROUGH that path.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location("precheck_eval_b3", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pe)

LWM = 3
APPLY = SimpleNamespace(apply=True)
DRY = SimpleNamespace(apply=False)


def _asp(goals, asp_id="asp-901", source="world", status="active"):
    return {"id": asp_id, "source": source, "status": status, "goals": goals}


def _g(gid, status, **kw):
    return {"id": gid, "status": status, **kw}


def _rec(gid, created_at, asp_id="asp-901", source="world", **kw):
    """One live `aspirations-query.sh --goal-status candidate --full` row."""
    return {"goal_id": gid, "id": gid, "asp_id": asp_id, "source": source,
            "status": "candidate", "created_at": created_at, **kw}


def _config(enabled=True, batch=5, lwm=LWM):
    cfg = {"pipeline_low_water_mark": lwm}
    if enabled is not None:
        cfg["candidate_tier"] = {"enabled": enabled, "auto_promote_batch": batch}
    return cfg


# : every starvation promote stamps its §5 ledger row through the
# status write's ledger channel; groom.py's promote cap excludes exactly this key.
STAMP = ["--ledger-evidence", '{"promoted_by": "starvation-failsafe"}']


class FakeScripts:
    """Stands in for _run_script: answers the candidate read, records writes."""

    def __init__(self, records=(), read_rc=0, fail_writes=()):
        self.records = list(records)
        self.read_rc = read_rc
        self.fail_writes = set(fail_writes)
        self.calls = []

    def __call__(self, args, input_text=None, timeout=30):
        self.calls.append(list(args))
        if args[0] == "aspirations-query.sh":
            if self.read_rc:
                return "", "daemon unreachable", self.read_rc
            return json.dumps(self.records), "", 0
        if args[0] == "aspirations-update-goal.sh":
            if args[3] in self.fail_writes:
                return "", "refused", 1
            return "{}", "", 0
        raise AssertionError(f"unexpected script call {args}")

    @property
    def writes(self):
        return [c for c in self.calls if c[0] == "aspirations-update-goal.sh"]

    @property
    def reads(self):
        return [c for c in self.calls if c[0] == "aspirations-query.sh"]


@pytest.fixture
def scripts(monkeypatch):
    def _install(**kw):
        fake = FakeScripts(**kw)
        monkeypatch.setattr(pe, "_run_script", fake)
        return fake
    return _install


@pytest.fixture(autouse=True)
def decisions(monkeypatch):
    rows = []
    monkeypatch.setattr(pe, "log_script_decision",
                        lambda name, rec: rows.append((name, rec)))
    return rows


def _candidates(n, start_day=1):
    """n candidates in asp-901, created one day apart (oldest first)."""
    goals = [_g(f"g-901-{10 + i}", "candidate") for i in range(n)]
    recs = [_rec(f"g-901-{10 + i}", f"2026-09-{start_day + i:02d}T00:00:00")
            for i in range(n)]
    return goals, recs


# ── flag OFF: byte-identical to the legacy output ──────────────────────────

LEGACY_COMPACT = [_asp([
    _g("g-901-01", "pending"),
    _g("g-901-02", "candidate"),
    _g("g-901-03", "pending", deferred_until="2999-01-01T00:00:00"),
    _g("g-901-04", "pending", blocked_by=["g-901-09"]),
    _g("g-901-05", "completed"),
    _g("g-901-06", "pending", blocked_by="g-901-05"),
])]


@pytest.mark.parametrize("cfg", [
    {"pipeline_low_water_mark": LWM},                                     # no block
    _config(enabled=False),                                               # shipped OFF
    {"pipeline_low_water_mark": LWM, "candidate_tier": {"enabled": "true"}},  # not literally True
    {"pipeline_low_water_mark": LWM, "candidate_tier": ["enabled"]},      # not a mapping
], ids=["no-block", "enabled-false", "enabled-string", "non-mapping"])
@pytest.mark.parametrize("args", [APPLY, DRY], ids=["apply", "dry"])
def test_flag_off_is_byte_identical_to_the_legacy_output(scripts, cfg, args):
    fake = scripts()
    out = pe.cmd_pipeline_depth(args, cfg, LEGACY_COMPACT)
    expected = {"subcommand": "pipeline-depth",
                "summary": "pipeline-depth: healthy (3 executable)",
                "flags": [], "executable_count": 3, "threshold": LWM}
    assert json.dumps(out) == json.dumps(expected)
    assert fake.calls == []


def test_flag_off_thin_pipeline_is_unchanged(scripts):
    fake = scripts()
    out = pe.cmd_pipeline_depth(APPLY, {"pipeline_low_water_mark": 4}, LEGACY_COMPACT)
    expected = {"subcommand": "pipeline-depth",
                "summary": "pipeline-depth: thin (3 executable < 4)",
                "flags": ["thin_pipeline"], "executable_count": 3, "threshold": 4}
    assert json.dumps(out) == json.dumps(expected)
    assert fake.calls == []


# ── flag ON ────────────────────────────────────────────────────────────────

def test_healthy_counts_pending_only_and_reads_nothing_live(scripts):
    goals, _ = _candidates(2)
    compact = [_asp([_g(f"g-901-0{i}", "pending") for i in range(1, 4)] + goals)]
    fake = scripts()
    out = pe.cmd_pipeline_depth(APPLY, _config(), compact)
    assert out["flags"] == []
    assert out["executable_count"] == 3
    assert out["groomable_count"] == 2
    assert fake.calls == []


def test_candidates_are_not_executable_so_starvation_is_visible(scripts):
    """The flag-OFF count includes candidates (). With the flag ON it
    must not, or 0 pending + N candidates would read as healthy."""
    goals, recs = _candidates(4)
    fake = scripts(records=recs)
    out = pe.cmd_pipeline_depth(DRY, _config(), [_asp(goals)])
    assert out["executable_count"] == 0
    assert out["flags"] == ["starvation_promoted"]


def test_boundary_one_below_the_mark_promotes_exactly_the_deficit(scripts):
    pending = [_g("g-901-01", "pending"), _g("g-901-02", "pending")]
    goals = [_g("g-901-11", "candidate"), _g("g-901-12", "candidate"),
             _g("g-901-13", "candidate")]
    recs = [_rec("g-901-11", "2026-09-03T00:00:00"),
            _rec("g-901-12", "2026-09-01T00:00:00"),   # oldest
            _rec("g-901-13", "2026-09-02T00:00:00")]
    fake = scripts(records=recs)
    out = pe.cmd_pipeline_depth(APPLY, _config(), [_asp(pending + goals)])
    assert out["flags"] == ["starvation_promoted"]
    assert out["promote_limit"] == 1
    assert out["promoted"] == ["g-901-12"]
    assert fake.writes == [["aspirations-update-goal.sh", "--source", "world",
                            "g-901-12", "status", "pending", *STAMP]]


def test_at_the_mark_nothing_fires(scripts):
    goals, recs = _candidates(3)
    pending = [_g(f"g-901-0{i}", "pending") for i in range(1, 4)]
    fake = scripts(records=recs)
    out = pe.cmd_pipeline_depth(APPLY, _config(), [_asp(pending + goals)])
    assert out["flags"] == []
    assert fake.calls == []


def test_batch_cap_bounds_a_deep_deficit(scripts):
    goals, recs = _candidates(8)
    fake = scripts(records=list(reversed(recs)))   # read order must not matter
    out = pe.cmd_pipeline_depth(APPLY, _config(batch=5, lwm=10), [_asp(goals)])
    assert out["promote_limit"] == 5
    assert out["promoted"] == [f"g-901-{10 + i}" for i in range(5)]   # five oldest
    assert len(fake.writes) == 5


def test_deficit_caps_below_the_batch(scripts):
    goals, recs = _candidates(6)
    pending = [_g("g-901-01", "pending")]
    fake = scripts(records=recs)
    out = pe.cmd_pipeline_depth(APPLY, _config(batch=5), [_asp(pending + goals)])
    assert out["promote_limit"] == 2
    assert out["promoted"] == ["g-901-10", "g-901-11"]


def test_ties_on_created_at_break_by_goal_id(scripts):
    goals = [_g("g-901-21", "candidate"), _g("g-901-20", "candidate")]
    recs = [_rec("g-901-21", "2026-09-01T00:00:00"),
            _rec("g-901-20", "2026-09-01T00:00:00")]
    scripts(records=recs)
    out = pe.cmd_pipeline_depth(APPLY, _config(lwm=1), [_asp(goals)])
    assert out["promoted"] == ["g-901-20"]


def test_a_candidate_that_would_stay_unexecutable_is_not_promoted(scripts):
    goals = [_g("g-901-30", "candidate"), _g("g-901-31", "candidate"),
             _g("g-901-32", "candidate")]
    recs = [_rec("g-901-30", "2026-09-01T00:00:00", deferred_until="2999-01-01T00:00:00"),
            _rec("g-901-31", "2026-09-02T00:00:00", blocked_by=["g-901-99"]),
            _rec("g-901-32", "2026-09-03T00:00:00")]
    scripts(records=recs)
    out = pe.cmd_pipeline_depth(APPLY, _config(), [_asp(goals)])
    assert out["groomable_count"] == 3
    assert out["promotable_count"] == 1
    assert out["promoted"] == ["g-901-32"]
    assert out["flags"] == ["starvation_promoted"]


def test_dry_run_reports_and_writes_nothing(scripts):
    goals, recs = _candidates(4)
    fake = scripts(records=recs)
    out = pe.cmd_pipeline_depth(DRY, _config(), [_asp(goals)])
    assert out["flags"] == ["starvation_promoted"]
    assert out["would_promote"] == ["g-901-10", "g-901-11", "g-901-12"]
    assert fake.writes == []
    assert "promoted" not in out


# ── the no-spin property (§4: generation only when groomable == 0) ─────────

@pytest.mark.parametrize("shape", ["ready", "all-deferred", "all-blocked"])
def test_zero_executable_with_candidates_never_reaches_generation(scripts, shape):
    goals, recs = _candidates(4)
    for r in recs:
        if shape == "all-deferred":
            r["deferred_until"] = "2999-01-01T00:00:00"
        elif shape == "all-blocked":
            r["blocked_by"] = ["g-901-99"]
    scripts(records=recs)
    for args in (APPLY, DRY):
        out = pe.cmd_pipeline_depth(args, _config(), [_asp(goals)])
        assert "thin_pipeline" not in out["flags"], (shape, out)
        assert out["flags"] == ["starvation_promoted"]


def test_simulated_starving_agent_gets_promoted_work_before_any_generation(scripts):
    """Iterate precheck the way the loop does: promotions land as pending, and
    the agent climbs back to the mark without thin_pipeline ever firing."""
    goals, recs = _candidates(5)
    compact = [_asp(goals)]
    by_id = {g["id"]: g for g in goals}
    seen_flags = []
    for _iteration in range(4):
        live = [r for r in recs if by_id[r["goal_id"]]["status"] == "candidate"]
        scripts(records=live)
        out = pe.cmd_pipeline_depth(APPLY, _config(), compact)
        seen_flags.append(out["flags"])
        for gid in out.get("promoted", []):
            by_id[gid]["status"] = "pending"      # the write landed
    assert all("thin_pipeline" not in f for f in seen_flags), seen_flags
    assert seen_flags[0] == ["starvation_promoted"]
    assert seen_flags[1] == []                    # back at the mark
    assert [g["id"] for g in goals if g["status"] == "pending"] == \
        ["g-901-10", "g-901-11", "g-901-12"]      # the three oldest


def test_no_groomable_candidate_keeps_the_legacy_generation_path(scripts):
    fake = scripts(records=[])
    out = pe.cmd_pipeline_depth(APPLY, _config(), [_asp([_g("g-901-01", "pending")])])
    assert out["flags"] == ["thin_pipeline"]
    assert out["groomable_count"] == 0
    assert fake.writes == []


def test_the_live_read_decides_over_a_stale_compact(scripts):
    _goals, recs = _candidates(2)
    scripts(records=recs)        # compact shows 0 candidates, the store has 2
    out = pe.cmd_pipeline_depth(APPLY, _config(), [_asp([])])
    assert out["groomable_count"] == 2
    assert out["flags"] == ["starvation_promoted"]
    assert out["promoted"] == ["g-901-10", "g-901-11"]


# ── fail open toward life (I1) ─────────────────────────────────────────────

def test_unreadable_candidates_fall_back_to_generation(scripts):
    goals, _ = _candidates(3)
    scripts(read_rc=1)
    out = pe.cmd_pipeline_depth(APPLY, _config(), [_asp(goals)])
    assert out["flags"] == ["thin_pipeline"]
    assert "rc=1" in out["failsafe_error"]


def test_every_promotion_failing_falls_back_to_generation(scripts):
    goals, recs = _candidates(3)
    scripts(records=recs, fail_writes={r["goal_id"] for r in recs})
    out = pe.cmd_pipeline_depth(APPLY, _config(), [_asp(goals)])
    assert out["flags"] == ["thin_pipeline"]
    assert out["promoted"] == []
    assert [f["goal_id"] for f in out["promote_failed"]] == ["g-901-10", "g-901-11", "g-901-12"]
    assert "failed" in out["failsafe_error"]


def test_a_partial_failure_still_counts_as_promoted_and_reports_the_rest(scripts):
    goals, recs = _candidates(3)
    scripts(records=recs, fail_writes={"g-901-11"})
    out = pe.cmd_pipeline_depth(APPLY, _config(), [_asp(goals)])
    assert out["flags"] == ["starvation_promoted"]
    assert out["promoted"] == ["g-901-10", "g-901-12"]
    assert out["promote_failed"][0]["goal_id"] == "g-901-11"


# ── the ledgered write path and the audit record ───────────────────────────

def test_promotions_go_through_the_status_update_path_with_their_own_source(scripts, decisions):
    world_goals = [_g("g-901-40", "candidate")]
    agent_goals = [_g("g-001-41", "candidate")]
    recs = [_rec("g-901-40", "2026-09-02T00:00:00"),
            _rec("g-001-41", "2026-09-01T00:00:00", asp_id="asp-001", source="agent")]
    compact = [_asp(world_goals), _asp(agent_goals, asp_id="asp-001", source="agent")]
    fake = scripts(records=recs)
    out = pe.cmd_pipeline_depth(APPLY, _config(), compact)
    assert fake.writes == [
        ["aspirations-update-goal.sh", "--source", "agent", "g-001-41", "status", "pending", *STAMP],
        ["aspirations-update-goal.sh", "--source", "world", "g-901-40", "status", "pending", *STAMP],
    ]
    assert out["promoted_by"] == "starvation-failsafe"
    (name, row), = decisions
    assert name == "precheck-eval"
    assert row["promoted_by"] == "starvation-failsafe"
    assert row["promoted"] == ["g-001-41", "g-901-40"]


def test_the_stamp_is_the_key_groom_excludes_from_the_promote_cap(tmp_path, monkeypatch):
    """ / rb-2036: the stamp written here and the key groom.py's promote cap
    excludes are two literals in two files. If they drift, a starvation promote counts
    against a grooming budget again and nothing errors (zeta's contract point (a)).
    Pin the real row the write path builds for this stamp to the real cap predicate,
    with the unstamped row as the control."""
    import datetime

    import groom
    from gates import candidate_transition as ct

    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    assert pe.STARVATION_PROMOTED_BY == groom.STARVATION_PROMOTER
    _verdict, evidence, err = ct.caller_channel(
        "pending", {"promoted_by": pe.STARVATION_PROMOTED_BY})
    assert err is None, err
    assert ct.append_ledger(tmp_path, goal_id="g-901-60", new_status="pending",
                            agent="agent-a", evidence=evidence) is True
    row = json.loads((tmp_path / ct.LEDGER_NAME).read_text(encoding="utf-8").splitlines()[0])
    row["ts"] = "2026-10-01T11:00:00"   # inside the window below; the clock-stamped ts is not the point
    since = datetime.datetime(2026, 9, 30, 16, 0, 0)
    assert groom.promotes_in_window([row], "agent-a", since) == 0, (
        "a starvation promote must not count against the grooming budget")
    plain = dict(row, evidence={k: v for k, v in row["evidence"].items() if k != "promoted_by"})
    assert groom.promotes_in_window([plain], "agent-a", since) == 1, (
        "control: the same row without the stamp is a grooming promote")


def test_candidates_outside_an_active_aspiration_are_not_promoted(scripts):
    goals = [_g("g-901-50", "candidate")]
    recs = [_rec("g-901-50", "2026-09-02T00:00:00"),
            _rec("g-902-51", "2026-09-01T00:00:00", asp_id="asp-902"),      # not active here
            _rec("g-901-52", "2026-09-01T00:00:00", source="agent")]        # agent asp-901 not active
    scripts(records=recs)
    out = pe.cmd_pipeline_depth(APPLY, _config(), [_asp(goals)])
    assert out["groomable_count"] == 1
    assert out["promoted"] == ["g-901-50"]


def test_the_shipped_config_arms_cleanly_when_switched_on(scripts):
    """B6 flips only `enabled`, so the rest of the shipped §8 block must already
    satisfy the fail-safe. Deliberately does NOT pin the flag's shipped value
    (g-353-164: a test pinning it turns red at activation)."""
    import yaml
    cfg = yaml.safe_load((SCRIPT_DIR.parent / "config" / "aspirations.yaml")
                         .read_text(encoding="utf-8"))
    cfg["candidate_tier"] = {**cfg["candidate_tier"], "enabled": True}
    goals, recs = _candidates(2)
    scripts(records=recs)
    out = pe.cmd_pipeline_depth(DRY, cfg, [_asp(goals)])
    assert out["candidate_tier"] == "on"
    assert out["flags"] == ["starvation_promoted"]
    assert out["would_promote"] == ["g-901-10", "g-901-11"]


@pytest.mark.parametrize("batch", [0, -1, None, True, "5"])
def test_a_bad_batch_size_fails_loud_when_armed(scripts, batch):
    scripts()
    cfg = {"pipeline_low_water_mark": LWM,
           "candidate_tier": {"enabled": True, "auto_promote_batch": batch}}
    with pytest.raises(KeyError):
        pe.cmd_pipeline_depth(APPLY, cfg, [_asp([])])


def test_loop_entry_runs_precheck_eval_with_apply():
    """The fail-safe's promote only fires under --apply; the medium battery is
    what runs precheck-eval at loop entry, so its lane must pass it."""
    bspec = importlib.util.spec_from_file_location(
        "precheck_medium_battery_b3", SCRIPT_DIR / "precheck-medium-battery.py")
    battery = importlib.util.module_from_spec(bspec)
    bspec.loader.exec_module(battery)
    lane, = [ln for ln in battery.LANES if ln["name"] == "precheck-eval"]
    assert lane["apply_flag"] is True
