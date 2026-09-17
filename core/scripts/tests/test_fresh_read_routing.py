"""Routed archive readers refresh before they read (, guard-6878).

Each test puts a NEWER store copy of an aspirations/pipeline archive behind a
stale (or absent) local mirror and asserts the reader's result reflects the
store copy. A reader that drops its refresh_for_read call reads the stale
mirror and fails. Every test also checks the stale state first (the positive
control), so a green run cannot come from a mirror that was already current.

Covered here: _frontier, _competence, gates/aspiration_supply,
team-state-sync-blockers, chronic-friction-aggregator, wm-contamination-check,
inactivity-detector, felt-sense-cadence-check, fresh-eyes-cadence-check,
aspirations._check_not_archived (g-358-125); hypothesis-capitalization,
work-alignment, gates/defer_target_existence, goal-resolve,
board-citation-check, displaced-id-audit, insight-trigger-sweep (its _refresh
reads live queues, g-358-130); and the main-style
readers goal-selector, consolidation-precheck, experience-reconcile,
goal-script-orphan-gate and cold_snapshot (g-358-130).
"""
import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))  # core/scripts
# Bind _fileops.get_backend before any test patches storage_backend.get_backend
# (see test_fresh_read.py for the measured leak this prevents).
import _fileops  # noqa: E402,F401
import cold_snapshot  # noqa: E402
import wm  # noqa: E402

# goal-selector derives AGENT_DIR at import, so an agent name is bound for the
# import only (same idiom as test_goal_selector_deadline_urgency.py).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "zeta")
goal_selector = importlib.import_module("goal-selector")
if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


def _load(filename: str, modname: str):
    spec = importlib.util.spec_from_file_location(modname, str(SCRIPT_DIR / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(p: Path, recs):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def _ids(path: Path):
    if not path.exists():
        return set()
    return {g["id"] for ln in path.read_text().splitlines() if ln.strip()
            for g in json.loads(ln).get("goals", [])}


class _PathStore:
    """Own-cloud stand-in keyed by PATH (world and agent archives share a
    basename). _roots lets the real owncloud_sync.refresh_would_clobber
    classify each path."""
    def __init__(self, roots, store):
        self._roots = roots
        self.store = {Path(k): v for k, v in store.items()}
        self.refreshed = []

    def refresh(self, path):
        path = Path(path)
        self.refreshed.append(path)
        if path in self.store:
            _write(path, self.store[path])


@pytest.fixture
def tree(tmp_path, monkeypatch):
    world = tmp_path / "world"
    agents = tmp_path / "agents"
    agent = agents / "zeta"
    world.mkdir()
    agent.mkdir(parents=True)
    monkeypatch.delenv("MIND_AGENT", raising=False)

    def install(store):
        import storage_backend
        be = _PathStore([(str(world.resolve()), "world"),
                         (str(agents.resolve()), "agents")], store)
        monkeypatch.setattr(storage_backend, "get_backend", lambda: be)
        return be
    return world, agent, install


def _asp(asp_id, *goals):
    return {"id": asp_id, "status": "completed", "goals": list(goals)}


def _goal(gid, **kw):
    return dict({"id": gid, "status": "completed"}, **kw)


def _stale_world_archive(world, install, extra_goal):
    """Local world archive holds g-1-01; the store copy adds `extra_goal`."""
    arch = world / "aspirations-archive.jsonl"
    local = [_asp("asp-1", _goal("g-1-01", completed_by="zeta",
                                 completed_date="2026-09-01T00:00:00"))]
    _write(arch, local)
    install({arch: local + [_asp("asp-2", extra_goal)]})
    assert extra_goal["id"] not in _ids(arch)  # POSITIVE CONTROL: mirror is stale
    return arch


def test_frontier_goal_index_sees_store_only_archive_goals(tree):
    world, agent, install = tree
    w_arch = world / "aspirations-archive.jsonl"
    a_arch = agent / "aspirations-archive.jsonl"
    _write(w_arch, [_asp("asp-1", _goal("g-1-01"))])
    be = install({w_arch: [_asp("asp-1", _goal("g-1-01")), _asp("asp-2", _goal("g-2-01"))],
                  a_arch: [_asp("asp-3", _goal("g-3-01"))]})
    assert "g-2-01" not in _ids(w_arch) and not a_arch.exists()  # POSITIVE CONTROL
    import _frontier
    goal_index, _asps, _stats = _frontier.load_goal_index(world, agent.parent)
    assert {"g-1-01", "g-2-01", "g-3-01"} <= set(goal_index)
    assert a_arch in be.refreshed  # an agent-queue archive is refreshed too


def test_competence_counts_store_only_archived_completions(tree):
    world, agent, install = tree
    _stale_world_archive(world, install, _goal("g-2-01", completed_by="zeta"))
    import _competence
    assert _competence.count_agent_completions(world, agent, "zeta") == 2


def test_aspiration_supply_load_existing_sees_store_only_archive(tree):
    world, agent, install = tree
    _stale_world_archive(world, install, _goal("g-2-01"))
    from gates import aspiration_supply
    ids = {r.get("id") for r in aspiration_supply.load_existing(world, agent_dirs=[agent])}
    assert "asp-2" in ids


def test_team_state_sync_blockers_statuses_include_store_only_archive(tree, monkeypatch):
    world, agent, install = tree
    _stale_world_archive(world, install, _goal("g-2-01"))
    mod = _load("team-state-sync-blockers.py", "tssb_fresh_read_routing")
    monkeypatch.setattr(mod, "WORLD_DIR", world)
    assert mod.load_goal_statuses().get("g-2-01") == "completed"


def test_chronic_friction_reads_store_only_archive(tree):
    world, agent, install = tree
    arch = _stale_world_archive(world, install, _goal("g-2-01"))
    mod = _load("chronic-friction-aggregator.py", "cfa_fresh_read_routing")
    assert "asp-2" in {a.get("id") for a in mod._read_aspirations(arch)}


def test_wm_contamination_ownership_index_sees_store_only_archive(tree):
    world, agent, install = tree
    _stale_world_archive(world, install, _goal("g-2-01", completed_by="zeta"))
    mod = _load("wm-contamination-check.py", "wmcc_fresh_read_routing")
    assert "g-2-01" in mod._build_ownership_index(world, agent)


def test_inactivity_detector_latest_completion_uses_store_archive(tree, monkeypatch):
    world, agent, install = tree
    _stale_world_archive(world, install,
                         _goal("g-2-01", completed_date="2026-09-15T00:00:00"))
    mod = _load("inactivity-detector.py", "inact_fresh_read_routing")
    monkeypatch.setattr(mod, "WORLD_DIR", world)
    monkeypatch.setattr(mod, "AGENT_DIR", None)
    _dt, gid = mod._latest_goal_completion()
    assert gid == "g-2-01"


@pytest.mark.parametrize("filename", ["felt-sense-cadence-check.py", "fresh-eyes-cadence-check.py"])
def test_cadence_completed_count_includes_store_only_archive(tree, monkeypatch, filename):
    world, agent, install = tree
    _stale_world_archive(world, install, _goal("g-2-01"))
    mod = _load(filename, filename[:-3].replace("-", "_") + "_fresh_read_routing")
    monkeypatch.setattr(mod._paths, "WORLD_DIR", world)
    assert mod.count_completed_goals() == 2


def test_check_not_archived_refuses_an_id_only_the_store_archive_holds(tree, monkeypatch):
    world, agent, install = tree
    arch = world / "aspirations-archive.jsonl"
    _write(arch, [])
    install({arch: [_asp("asp-7", _goal("g-7-01"))]})
    import aspirations
    monkeypatch.setattr(aspirations, "ARCHIVE_PATH", arch)
    monkeypatch.setattr(aspirations, "LIVE_PATH", world / "aspirations.jsonl")
    with pytest.raises(SystemExit):
        aspirations._check_not_archived("asp-7", action="add")


def _stale_pipeline_archive(world, install, local_ids, store_ids, **fields):
    arch = world / "pipeline-archive.jsonl"
    _write(arch, [dict({"id": i}, **fields) for i in local_ids])
    install({arch: [dict({"id": i}, **fields) for i in store_ids]})
    on_disk = {json.loads(ln)["id"] for ln in arch.read_text().splitlines() if ln.strip()}
    assert not (set(store_ids) - set(local_ids)) & on_disk  # POSITIVE CONTROL: mirror is stale
    return arch


def test_hypothesis_capitalization_corpus_includes_store_only_archive(tree):
    world, agent, install = tree
    mod = _load("hypothesis-capitalization.py", "hypcap_fresh_read_routing")
    _stale_pipeline_archive(world, install, ["h-1"], ["h-1", "h-2"], outcome="CONFIRMED")
    records, _diag = mod.load_corpus(world)
    assert "h-2" in records


def test_work_alignment_novelty_history_reads_store_only_archive(tree, monkeypatch):
    world, agent, install = tree
    mod = _load("work-alignment.py", "wa_fresh_read_routing")
    arch = _stale_world_archive(world, install, _goal("g-2-01"))
    seen = {}

    def spy(active, archive):
        seen["ids"] = {a.get("id") for a in archive}
        return 0

    monkeypatch.setattr(mod, "read_self", lambda: "# Self\n")
    monkeypatch.setattr(mod, "ASP_PATH", world / "aspirations.jsonl")
    monkeypatch.setattr(mod, "ASP_ARCHIVE_PATH", arch)
    monkeypatch.setattr(mod, "compute_hours_since_novel", spy)
    mod.cmd_check(argparse.Namespace(ranked_goals=None))
    assert "asp-2" in seen["ids"]


def test_defer_target_existence_knows_goal_only_the_store_archive_holds(tree):
    world, agent, install = tree
    from gates import defer_target_existence as dte
    _stale_world_archive(world, install, _goal("g-2-01"))
    assert "g-2-01" in dte.known_goal_ids(dte.sources_for(world, None))


def test_goal_resolve_finds_goal_only_the_store_archive_holds(tree):
    world, agent, install = tree
    mod = _load("goal-resolve.py", "goal_resolve_fresh_read_routing")
    _stale_world_archive(world, install, _goal("g-2-01"))
    assert mod.resolve("g-2-01", world=str(world))["disposition"] == "archived"


def test_board_citation_check_loads_ids_from_store_only_board_archive(tree):
    world, agent, install = tree
    mod = _load("board-citation-check.py", "bcc_fresh_read_routing")
    arch = world / "board" / "findings-archive.jsonl"
    _write(arch, [{"id": "msg-1"}])
    install({arch: [{"id": "msg-1"}, {"id": "msg-2"}]})
    assert "msg-2" not in arch.read_text()  # POSITIVE CONTROL
    assert "msg-2" in mod.load_board_ids(world)


def test_insight_trigger_sweep_converted_ids_include_store_only_goal(tree, monkeypatch):
    # insight-trigger-sweep._refresh reads the LIVE queues, not an archive.
    world, agent, install = tree
    mod = _load("insight-trigger-sweep.py", "its_fresh_read_routing")
    asps = world / "aspirations.jsonl"
    local = [_asp("asp-1", _goal("g-1-01", origin_signal="insight_trigger:msg-1"))]
    _write(asps, local)
    install({asps: local + [_asp("asp-2", _goal("g-2-01", origin_signal="insight_trigger:msg-2"))]})
    assert "msg-2" not in asps.read_text()  # POSITIVE CONTROL: mirror is stale
    monkeypatch.setattr(mod, "WORLD_ASPS", asps)
    monkeypatch.setattr(mod, "_agents_root", lambda: agent.parent)
    assert "msg-2" in mod.load_converted_ids()


def test_displaced_id_audit_collects_pair_from_store_only_reid_archive(tree, tmp_path):
    world, agent, install = tree
    mod = _load("displaced-id-audit.py", "dia_fresh_read_routing")
    meta = tmp_path / "meta"
    meta.mkdir()
    arch = world / "guardrails-archive.jsonl"
    kept = {"id": "guard-9", "rule": "kept"}
    _write(arch, [kept])
    install({arch: [kept, {"id": "guard-10", "rule": "moved", "displaced_from": "guard-7"}]})
    assert "guard-10" not in arch.read_text()  # POSITIVE CONTROL
    pairs, _occ, _stats = mod.collect(world, meta)
    assert ("guard-7", "guard-10") in {(p["old"], p["new"]) for p in pairs}


def test_goal_selector_select_reads_store_only_pipeline_archive(tree, monkeypatch):
    world, agent, install = tree
    gs = goal_selector
    arch = _stale_pipeline_archive(world, install, ["h-1"], ["h-1", "h-2"], outcome="CONFIRMED")
    seen = {}

    class _Stop(Exception):
        pass

    def fake_read(path):
        path = Path(path)
        if path == arch:  # record what the archive read sees, then stop
            seen["ids"] = {json.loads(ln)["id"] for ln in path.read_text().splitlines() if ln.strip()}
            raise _Stop
        if path == world / "aspirations.jsonl":
            return [_asp("asp-1", _goal("g-1-01", status="pending"))]
        return []

    monkeypatch.setattr(gs, "refresh_aspiration_caches", lambda *a, **k: None)
    monkeypatch.setattr(gs, "read_jsonl", fake_read)
    monkeypatch.setattr(gs, "WORLD_ASP_PATH", world / "aspirations.jsonl")
    monkeypatch.setattr(gs, "AGENT_ASP_PATH", None)
    monkeypatch.setattr(gs, "PIPELINE_PATH", world / "pipeline.jsonl")
    monkeypatch.setattr(gs, "PIPELINE_ARCHIVE_PATH", arch)
    with pytest.raises(_Stop):
        gs.cmd_select(None)
    assert "h-2" in seen["ids"]


def test_consolidation_precheck_unreflected_counts_store_only_archive(tree, monkeypatch, capsys):
    world, agent, install = tree
    mod = _load("consolidation-precheck.py", "cp_fresh_read_routing")
    _stale_pipeline_archive(world, install, ["h-1"], ["h-1", "h-2"],
                            stage="archived", reflected=False, outcome="CONFIRMED")
    monkeypatch.setattr(wm, "wm_path", lambda *a, **k: agent / "session" / "working-memory.yaml")
    monkeypatch.setattr(mod, "WORLD_DIR", world)
    monkeypatch.setattr(mod, "AGENT_DIR", agent)
    mod.main()
    assert json.loads(capsys.readouterr().out)["unreflected"] == 2


def test_experience_reconcile_goal_index_includes_store_only_archives(tree, monkeypatch):
    world, agent, install = tree
    mod = _load("experience-reconcile.py", "er_fresh_read_routing")
    a_arch = agent / "aspirations-archive.jsonl"
    w_arch = world / "aspirations-archive.jsonl"
    _write(a_arch, [_asp("asp-1", _goal("g-1-01"))])
    _write(w_arch, [_asp("asp-3", _goal("g-3-01"))])
    (agent / "local-paths.conf").write_text(f"WORLD_PATH={world}\n", encoding="utf-8")
    install({a_arch: [_asp("asp-1", _goal("g-1-01")), _asp("asp-2", _goal("g-2-01"))],
             w_arch: [_asp("asp-3", _goal("g-3-01")), _asp("asp-4", _goal("g-4-01"))]})
    assert "g-2-01" not in _ids(a_arch) and "g-4-01" not in _ids(w_arch)  # POSITIVE CONTROL
    monkeypatch.setattr(mod, "discover_agents", lambda: ["zeta"])
    monkeypatch.setattr(mod, "_agent_dir", lambda name: agent)
    index = mod.load_goal_index()
    assert {"g-2-01", "g-4-01"} <= set(index)  # one per refresh call


def test_goal_script_orphan_gate_scans_store_only_archive(tree, monkeypatch, capsys):
    world, agent, install = tree
    mod = _load("goal-script-orphan-gate.py", "gsog_fresh_read_routing")
    _stale_world_archive(world, install, _goal(
        "g-2-01", status="pending", description="run core/scripts/ghost-script.sh"))
    monkeypatch.setattr(mod, "WORLD_DIR", world)
    monkeypatch.setattr(mod, "AGENT_DIR", None)
    monkeypatch.setattr(mod, "_existing_scripts", lambda: set())
    monkeypatch.setattr(sys, "argv", ["goal-script-orphan-gate.py",
                                      "--include-archived", "--include-completed-goals"])
    with pytest.raises(SystemExit):
        mod.main()
    report = json.loads(capsys.readouterr().out)
    assert "g-2-01" in {o["goal_id"] for o in report["orphan_references"]}


def test_cold_snapshot_manifest_hashes_the_store_copy_of_an_archive(tree, monkeypatch):
    world, agent, install = tree
    arch = world / "aspirations-archive.jsonl"
    local = [_asp("asp-1", _goal("g-1-01"))]
    store = local + [_asp("asp-2", _goal("g-2-01"))]
    _write(arch, local)
    install({arch: store})
    assert "g-2-01" not in _ids(arch)  # POSITIVE CONTROL
    expected = "".join(json.dumps(r) + "\n" for r in store).encode("utf-8")
    monkeypatch.setattr(cold_snapshot, "_roots", lambda: [(world, "world")])
    entries, _total = cold_snapshot.build_manifest(include_archives=True)
    entry = next(e for e in entries if e["path"] == "world/aspirations-archive.jsonl")
    assert entry["sha256"] == hashlib.sha256(expected).hexdigest()
