"""test_recurring_rehome_on_archive.py — : recurring goals are
UN-STRANDABLE at the archive boundary.

Measured 2026-08-30 on a downstream deployment: an intent sweep archived
aspirations that still carried PENDING RECURRING goals. Nothing could notice —
the goal selector never reads aspirations-archive.jsonl and all three
cadence-liveness instruments are scoped to the selectable queue — so the
rituals silently stopped. The archive-time invariant is therefore the ONLY
possible detector, and this file pins it on every archive path plus the
one-shot backfill for aspirations archived before the invariant existed:

  complete(force=true)   re-homes, never strands
  retire(force=true)     re-homes, never strands
  complete-intent        re-homes (this is the path that stranded downstream)
  no live container      the archive is REFUSED (even under force), nothing written
  explicit rehome_target wins over auto-detection; a dead explicit target refuses
  backfill               already-archived rows are re-homed once, idempotently;
                         dry_run writes nothing

Hermetic: in-process DaemonFixture (no daemon_integration marker).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
from _daemon_fixture import DaemonFixture  # noqa: E402
sys.path.insert(0, str(TESTS.parent))
import _rt  # noqa: E402

ASP = "asp-900"
HOME = "asp-800"


def _goal(gid: str, status: str, recurring: bool = False) -> dict:
    g = {
        "id": gid, "title": f"Goal {gid}", "description": "rehome invariant probe",
        "status": status, "priority": "MEDIUM", "recurring": recurring,
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive", "participants": ["agent"],
    }
    if recurring:
        g["interval_hours"] = 24
        g["lastAchievedAt"] = "2026-08-30T00:00:00"
    return g


def _asp(asp_id: str, status: str, goals: list[dict], **extra) -> dict:
    d = {"id": asp_id, "title": f"Asp {asp_id}", "motivation": "Test", "scope": "sprint",
         "priority": "MEDIUM", "status": status, "created": "2026-07-22T00:00:00",
         "goals": goals}
    d.update(extra)
    return d


def _seed_world(tmp: Path, live: list[dict], archived: list[dict] = ()) -> Path:
    world = tmp / "world"
    world.mkdir(parents=True, exist_ok=True)
    (world / "aspirations.jsonl").write_text(
        "".join(json.dumps(a, ensure_ascii=False) + "\n" for a in live), encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text(
        "".join(json.dumps(a, ensure_ascii=False) + "\n" for a in archived), encoding="utf-8")
    return world


def _rows(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _live(world: Path) -> dict:
    return {a["id"]: a for a in _rows(world / "aspirations.jsonl")}


def _archive(world: Path) -> dict:
    return {a["id"]: a for a in _rows(world / "aspirations-archive.jsonl")}


def _goal_in(asp: dict, gid: str) -> dict | None:
    return next((g for g in asp.get("goals", []) if g.get("id") == gid), None)


def _seed_intent_config(project_root: Path) -> None:
    cfg_dir = project_root / "core" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "aspirations.yaml").write_text(
        "intent_satisfaction:\n  min_evidence_by_scope:\n    sprint: 1\n    project: 1\n"
        "    initiative: 1\nrecurring:\n  rehome_container: null\n", encoding="utf-8")


def _call(method: str, path: str, query: str, body: str | None = None):
    """rt_call returns the raw body string on 2xx and raises RtError on 4xx/5xx."""
    raw = _rt.rt_call(method, path, query=query, body=body)
    return json.loads(raw) if raw and raw.strip().startswith("{") else raw


def _err_code(exc_or_resp) -> str:
    # RtError carries the daemon's JSON error body on .body; the message alone
    # is only "daemon HTTP 400 for POST <path>".
    text = "%s %s" % (exc_or_resp, getattr(exc_or_resp, "body", "") or "")
    for code in ("recurring_rehome_target_missing", "recurring_goals_present"):
        if code in text:
            return code
    return text


def _post_expect_error(path: str, query: str, body: str | None = None) -> str:
    try:
        resp = _call("POST", path, query, body)
    except Exception as exc:  # noqa: BLE001 - rt_call raises on 4xx
        return _err_code(exc)
    return _err_code(resp)


def _assert_rehomed(world: Path, gid: str, src: str = ASP, home: str = HOME) -> None:
    live = _live(world)
    assert src not in live, f"{src} must have left the live queue"
    adopted = _goal_in(live[home], gid)
    assert adopted is not None, f"{gid} must now live in {home}"
    assert adopted["recurring"] is True and adopted["status"] == "pending"
    assert adopted["rehomed_from"] == src and adopted.get("rehomed_at")
    assert "g-357-31" in adopted["rehome_reason"]
    left = _goal_in(_archive(world)[src], gid)
    assert left is not None, "the archived row keeps a pointer copy (never dropped)"
    assert left["status"] == "superseded" and left["recurring"] is False
    assert left["rehomed_to"] == home and left["superseded_by_goal"] == gid


# ───────────────────────── archive-time invariant ─────────────────────────

def test_retire_force_rehomes_recurring_goals_into_the_live_container():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), live=[
            _asp(ASP, "active", [_goal("g-900-01", "pending", recurring=True),
                                 _goal("g-900-02", "completed")]),
            _asp(HOME, "active", [_goal("g-800-01", "pending", recurring=True)]),
        ])
        with DaemonFixture(world):
            _call("POST", "/v1/aspirations/retire", f"asp_id={ASP}&source=world&force=true")
        _assert_rehomed(world, "g-900-01")
        assert _goal_in(_archive(world)[ASP], "g-900-02")["status"] == "completed"


def test_complete_force_rehomes_recurring_goals():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), live=[
            _asp(ASP, "active", [_goal("g-900-01", "pending", recurring=True),
                                 _goal("g-900-02", "completed")]),
            _asp(HOME, "active", [_goal("g-800-01", "pending", recurring=True)]),
        ])
        with DaemonFixture(world):
            _call("POST", "/v1/aspirations/complete", f"asp_id={ASP}&source=world&force=true")
        _assert_rehomed(world, "g-900-01")


def test_complete_intent_rehomes_instead_of_stranding():
    """The downstream incident path: an intent-satisfied completion of an
    aspiration that still carries a live recurring goal."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), live=[
            _asp(ASP, "active", [_goal("g-900-10", "completed"),
                                 _goal("g-900-11", "pending"),
                                 _goal("g-900-12", "pending", recurring=True)]),
            _asp(HOME, "active", [_goal("g-800-01", "pending", recurring=True)]),
        ])
        intent = {"evidence_goal_ids": ["g-900-10"], "superseded_goal_ids": ["g-900-11"],
                  "rationale": "Test intent is satisfied by the completed evidence goal."}
        with DaemonFixture(world) as df:
            _seed_intent_config(df.project_root)
            _call("POST", "/v1/aspirations/complete-intent", f"asp_id={ASP}&source=world",
                  body=json.dumps(intent))
        _assert_rehomed(world, "g-900-12")
        assert _goal_in(_archive(world)[ASP], "g-900-11")["status"] == "superseded"


def test_archive_is_refused_when_no_live_container_exists_even_under_force():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), live=[
            _asp(ASP, "active", [_goal("g-900-01", "pending", recurring=True)]),
            _asp("asp-801", "active", [_goal("g-801-01", "pending")]),  # no recurring, no flag
        ])
        before = (world / "aspirations.jsonl").read_text(encoding="utf-8")
        with DaemonFixture(world):
            for path, query in (("/v1/aspirations/retire", f"asp_id={ASP}&source=world&force=true"),
                                ("/v1/aspirations/complete", f"asp_id={ASP}&source=world&force=true")):
                assert _post_expect_error(path, query) == "recurring_rehome_target_missing"
        assert (world / "aspirations.jsonl").read_text(encoding="utf-8") == before
        assert _archive(world) == {}, "nothing may reach the archive on a refusal"


def test_explicit_rehome_target_wins_and_a_dead_one_refuses():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), live=[
            _asp(ASP, "active", [_goal("g-900-01", "pending", recurring=True)]),
            _asp(HOME, "active", [_goal("g-800-01", "pending", recurring=True),
                                  _goal("g-800-02", "pending", recurring=True)]),
            _asp("asp-801", "active", [_goal("g-801-01", "pending")]),
        ])
        with DaemonFixture(world):
            assert _post_expect_error(
                "/v1/aspirations/retire",
                f"asp_id={ASP}&source=world&force=true&rehome_target=asp-999",
            ) == "recurring_rehome_target_missing"
            _call("POST", "/v1/aspirations/retire",
                  f"asp_id={ASP}&source=world&force=true&rehome_target=asp-801")
        _assert_rehomed(world, "g-900-01", home="asp-801")
        assert _goal_in(_live(world)[HOME], "g-900-01") is None, "auto-detect must not also adopt it"


def test_recurring_home_flag_beats_the_most_recurring_heuristic():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), live=[
            _asp(ASP, "active", [_goal("g-900-01", "pending", recurring=True)]),
            _asp(HOME, "active", [_goal("g-800-01", "pending", recurring=True),
                                  _goal("g-800-02", "pending", recurring=True)]),
            _asp("asp-802", "active", [_goal("g-802-01", "pending")], recurring_home=True),
        ])
        with DaemonFixture(world):
            _call("POST", "/v1/aspirations/retire", f"asp_id={ASP}&source=world&force=true")
        _assert_rehomed(world, "g-900-01", home="asp-802")


def test_unforced_paths_keep_the_recurring_goals_present_refusal():
    """The pre-existing refusal stays: without force, complete/retire still
    refuse an aspiration carrying recurring goals (nothing is re-homed)."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd), live=[
            _asp(ASP, "active", [_goal("g-900-01", "pending", recurring=True)]),
            _asp(HOME, "active", [_goal("g-800-01", "pending", recurring=True)]),
        ])
        with DaemonFixture(world):
            assert _post_expect_error("/v1/aspirations/retire", f"asp_id={ASP}&source=world") \
                == "recurring_goals_present"
        assert _goal_in(_live(world)[HOME], "g-900-01") is None
        assert ASP in _live(world)


# ───────────────────────── one-shot backfill ─────────────────────────

def test_backfill_rehomes_stranded_archived_recurring_goals_once():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(
            Path(tmpd),
            live=[_asp(HOME, "active", [_goal("g-800-01", "pending", recurring=True)])],
            archived=[
                _asp("asp-700", "completed", [_goal("g-700-01", "pending", recurring=True),
                                              _goal("g-700-02", "completed")], archived=True),
                _asp("asp-701", "retired", [_goal("g-701-01", "skipped")], archived=True),
                _asp("asp-702", "retired", [_goal("g-702-01", "in-progress", recurring=True)], archived=True),
            ])
        with DaemonFixture(world):
            first = _call("POST", "/v1/aspirations/rehome-recurring-backfill", "source=world")
            second = _call("POST", "/v1/aspirations/rehome-recurring-backfill", "source=world")
        assert first["moved"] == {"asp-700": ["g-700-01"], "asp-702": ["g-702-01"]}, first
        assert first["target"] == HOME and first["moved_count"] == 2
        assert first["archived_scanned"] == 3 and first["stranded_aspirations"] == 2
        assert second["moved_count"] == 0 and second["stranded_aspirations"] == 0, second
        live = _live(world)
        for gid, src in (("g-700-01", "asp-700"), ("g-702-01", "asp-702")):
            adopted = _goal_in(live[HOME], gid)
            assert adopted and adopted["recurring"] is True and adopted["rehomed_from"] == src
        arch = _archive(world)
        assert set(arch) == {"asp-700", "asp-701", "asp-702"}, "no archive row may be dropped"
        assert _goal_in(arch["asp-700"], "g-700-01")["status"] == "superseded"
        assert _goal_in(arch["asp-700"], "g-700-02")["status"] == "completed"
        assert _goal_in(arch["asp-702"], "g-702-01")["rehomed_to"] == HOME
        assert _goal_in(arch["asp-701"], "g-701-01")["status"] == "skipped"


def test_backfill_dry_run_writes_nothing_and_missing_container_refuses():
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(
            Path(tmpd),
            live=[_asp(HOME, "active", [_goal("g-800-01", "pending", recurring=True)])],
            archived=[_asp("asp-700", "completed", [_goal("g-700-01", "pending", recurring=True)], archived=True)])
        live_before = (world / "aspirations.jsonl").read_text(encoding="utf-8")
        arch_before = (world / "aspirations-archive.jsonl").read_text(encoding="utf-8")
        with DaemonFixture(world):
            plan = _call("POST", "/v1/aspirations/rehome-recurring-backfill", "source=world&dry_run=true")
        assert plan["dry_run"] is True and plan["moved"] == {"asp-700": ["g-700-01"]}
        assert (world / "aspirations.jsonl").read_text(encoding="utf-8") == live_before
        assert (world / "aspirations-archive.jsonl").read_text(encoding="utf-8") == arch_before

        world2 = _seed_world(
            Path(tmpd) / "two",
            live=[_asp("asp-801", "active", [_goal("g-801-01", "pending")])],
            archived=[_asp("asp-700", "completed", [_goal("g-700-01", "pending", recurring=True)], archived=True)])
        with DaemonFixture(world2):
            assert _post_expect_error("/v1/aspirations/rehome-recurring-backfill", "source=world") \
                == "recurring_rehome_target_missing"
        assert _goal_in(_archive(world2)["asp-700"], "g-700-01")["status"] == "pending"


def test_backfill_wrapper_and_route_are_wired():
    scripts = TESTS.parent
    wrapper = (scripts / "aspirations-rehome-recurring-backfill.sh").read_text(encoding="utf-8")
    assert "/v1/aspirations/rehome-recurring-backfill" in wrapper
    assert "rt_no_daemon_error" in wrapper and "_fallback_exec" not in wrapper
    src = (scripts.parent.parent / "mind_api" / "src" / "endpoints" / "aspirations_write.py").read_text(encoding="utf-8")
    assert 'routes[("POST", "/v1/aspirations/rehome-recurring-backfill")] = rehome_recurring_backfill' in src
    for verb in ('"complete")', '"retire")', '"complete-intent")'):
        assert f"_rehome_or_refuse(items, asp, asp_id, ctx, warnings, {verb}" in src, verb
