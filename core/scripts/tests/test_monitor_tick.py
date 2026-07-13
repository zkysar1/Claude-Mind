"""Tests for FW-1b monitor-tick (): monitor-tick.py + monitor-finding-convert.py.

Hermetic -- no live daemon, no real probe scripts. monitor-tick.run_tick exposes
`probe_runner` (canned rc/output) and `on_trip_fn` (recorder) injection seams;
monitor-finding-convert.convert_finding exposes `goals` (fake open-goal corpus) and
`filer` (fake daemon) seams. Proves the design's three mandated cases:
  (a) clean probe files nothing,
  (b) tripped probe files exactly one deduped goal,
  (c) allowlist-empty = inert,
plus the per-probe interval gate and the converter origin_signal dedup.
"""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent


def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TICK = _load("monitor_tick", "monitor-tick.py")
CONV = _load("monitor_finding_convert", "monitor-finding-convert.py")


# ---- registry helpers ------------------------------------------------------

def _write_registry(tmp_path, enabled, probes):
    import yaml
    p = tmp_path / "monitor-probes.yaml"
    p.write_text(yaml.safe_dump({"enabled_probes": enabled, "probes": probes}),
                 encoding="utf-8")
    return p


PROBE_P1 = {
    "id": "p1", "script": "core/scripts/does-not-exist.sh", "interval_hours": 24,
    "on_trip": "file_goal", "target_asp": "asp-115",
    "origin_signal_prefix": "monitor-probe",
}


# ---- (c) allowlist-empty = inert -------------------------------------------

def test_empty_allowlist_is_inert(tmp_path):
    reg = _write_registry(tmp_path, enabled=[], probes=[PROBE_P1])
    state = tmp_path / "state.json"
    calls = []
    res = TICK.run_tick(str(reg), str(state),
                        probe_runner=lambda *a: calls.append("ran") or (0, ""),
                        on_trip_fn=lambda *a: calls.append("trip") or {})
    assert res["inert"] is True
    assert calls == []            # no probe ran, no conversion
    assert not state.exists()     # nothing written


# ---- (a) clean probe files nothing -----------------------------------------

def test_clean_probe_files_nothing(tmp_path):
    reg = _write_registry(tmp_path, enabled=["p1"], probes=[PROBE_P1])
    state = tmp_path / "state.json"
    trips = []
    res = TICK.run_tick(str(reg), str(state),
                        probe_runner=lambda script, args: (0, "ok"),
                        on_trip_fn=lambda probe, ev: trips.append((probe, ev)) or {})
    assert res["clean"] == ["p1"]
    assert res["tripped"] == []
    assert res["filed"] == []
    assert trips == []            # converter NEVER called on a clean probe
    # last-run recorded so the interval gate advances.
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["probes"]["p1"]["last_outcome"] == "clean"
    assert st["probes"]["p1"]["last_run"]


# ---- (b) tripped probe calls the converter exactly once --------------------

def test_tripped_probe_converts_once(tmp_path):
    reg = _write_registry(tmp_path, enabled=["p1"], probes=[PROBE_P1])
    state = tmp_path / "state.json"
    trips = []

    def on_trip(probe, evidence):
        trips.append((probe["id"], evidence))
        return {"filed": True, "goal_id": "g-115-9001", "deduped": False}

    res = TICK.run_tick(str(reg), str(state),
                        probe_runner=lambda script, args: (1, "DRIFT DETECTED: 3 nodes"),
                        on_trip_fn=on_trip)
    assert res["tripped"] == ["p1"]
    assert res["clean"] == []
    assert len(trips) == 1                      # exactly once
    assert trips[0][0] == "p1"
    assert "DRIFT DETECTED" in trips[0][1]      # evidence forwarded
    assert res["filed"] == [{"probe": "p1", "goal_id": "g-115-9001"}]
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["probes"]["p1"]["last_outcome"] == "tripped"
    assert st["probes"]["p1"]["last_goal_id"] == "g-115-9001"


# ---- per-probe interval gate -----------------------------------------------

def test_interval_gate_skips_not_due(tmp_path):
    reg = _write_registry(tmp_path, enabled=["p1"], probes=[PROBE_P1])
    state = tmp_path / "state.json"
    now = dt.datetime(2026, 6, 22, 12, 0, 0)
    # last run 1h ago, interval 24h -> NOT due.
    state.write_text(json.dumps({"probes": {"p1": {
        "last_run": (now - dt.timedelta(hours=1)).isoformat()}}}), encoding="utf-8")
    ran = []
    res = TICK.run_tick(str(reg), str(state), now=now,
                        probe_runner=lambda *a: ran.append(1) or (0, ""),
                        on_trip_fn=lambda *a: {})
    assert res["skipped_not_due"] == ["p1"]
    assert res["ran"] == []
    assert ran == []


def test_interval_gate_runs_when_due(tmp_path):
    reg = _write_registry(tmp_path, enabled=["p1"], probes=[PROBE_P1])
    state = tmp_path / "state.json"
    now = dt.datetime(2026, 6, 22, 12, 0, 0)
    # last run 25h ago, interval 24h -> due.
    state.write_text(json.dumps({"probes": {"p1": {
        "last_run": (now - dt.timedelta(hours=25)).isoformat()}}}), encoding="utf-8")
    res = TICK.run_tick(str(reg), str(state), now=now,
                        probe_runner=lambda script, args: (0, "ok"),
                        on_trip_fn=lambda *a: {})
    assert res["ran"] == ["p1"]
    assert res["clean"] == ["p1"]


# ---- converter origin_signal dedup -----------------------------------------

def test_converter_files_when_no_open_goal():
    filed = []

    def filer(target_asp, record, source, osig):
        filed.append((target_asp, record["origin_signal"]))
        return "g-115-7777"

    res = CONV.convert_finding(PROBE_P1, "trip evidence", goals=[], filer=filer)
    assert res["filed"] is True
    assert res["deduped"] is False
    assert res["goal_id"] == "g-115-7777"
    assert filed == [("asp-115", "monitor-probe:p1")]


def test_converter_dedups_against_open_goal():
    filed = []
    open_goals = [{"status": "pending", "origin_signal": "monitor-probe:p1",
                   "title": "Investigate: monitor-tick probe p1 tripped"}]
    res = CONV.convert_finding(PROBE_P1, "trip evidence", goals=open_goals,
                               filer=lambda *a: filed.append(1) or "g-x")
    assert res["deduped"] is True
    assert res["filed"] is False
    assert filed == []            # daemon NEVER hit when a matching open goal exists


def test_converter_dedups_on_probe_id_in_text():
    # An open goal that references the probe id (no origin_signal) still suppresses.
    open_goals = [{"status": "in-progress", "origin_signal": "",
                   "description": "looking into the p1 sweep failure"}]
    res = CONV.convert_finding(PROBE_P1, "ev", goals=open_goals, filer=lambda *a: "g")
    assert res["deduped"] is True


def test_converter_record_is_valid(tmp_path):
    res = CONV.convert_finding(PROBE_P1, "evidence text", goals=[], dry_run=True)
    rec = res["record"]
    assert rec["origin_signal"] == "monitor-probe:p1"
    assert rec["participants"] == ["agent"]
    assert rec["category"] == "framework-architecture"
    assert rec["priority"] == "MEDIUM"
    assert "p1" in rec["title"]
    assert "monitor-tick" in rec["tags"]


# ---- integration: tripped -> files ONE deduped goal end-to-end -------------

def test_tripped_files_exactly_one_deduped_goal_end_to_end(tmp_path):
    """Two ticks of a probe that trips every time -> the REAL converter files
    exactly ONE goal (the 2nd tick dedups against the 1st's open goal)."""
    reg = _write_registry(tmp_path, enabled=["p1"], probes=[PROBE_P1])
    state = tmp_path / "state.json"
    filed = []
    corpus = []   # simulates the open-goal queue growing as goals are filed

    def filer(target_asp, record, source, osig):
        gid = "g-115-%d" % (8000 + len(filed))
        filed.append(gid)
        corpus.append({"status": "pending", "origin_signal": osig,
                       "title": record["title"]})
        return gid

    def on_trip(probe, evidence):
        # real converter, with the growing corpus + fake filer injected
        return CONV.convert_finding(probe, evidence, goals=list(corpus), filer=filer)

    base = dt.datetime(2026, 6, 22, 12, 0, 0)
    # tick 1 (due: no prior state) -> trips -> files goal #1
    r1 = TICK.run_tick(str(reg), str(state), now=base,
                       probe_runner=lambda s, a: (1, "trip"), on_trip_fn=on_trip)
    # tick 2, 25h later (due again) -> trips -> dedup (open goal carries origin_signal)
    r2 = TICK.run_tick(str(reg), str(state), now=base + dt.timedelta(hours=25),
                       probe_runner=lambda s, a: (1, "trip"), on_trip_fn=on_trip)

    assert len(r1["filed"]) == 1
    assert r2["filed"] == [] and r2["deduped"] == ["p1"]
    assert filed == ["0"]       # exactly ONE goal across both trips


# ---- E1: external-path resolution (world/ meta/ -> external dirs) ----------

def test_resolve_script_path_world_meta_external(tmp_path):
    pr = tmp_path / "proj"
    wd = tmp_path / "external-world"
    md = tmp_path / "external-meta"
    # world/ resolves under WORLD_DIR, NOT project_root/world
    assert TICK._resolve_script_path("world/scripts/x.sh", pr, wd, md) == wd / "scripts/x.sh"
    # meta/ resolves under META_DIR
    assert TICK._resolve_script_path("meta/probe.py", pr, wd, md) == md / "probe.py"
    # core/ (and every other relative path) resolves under project_root, unchanged
    assert TICK._resolve_script_path("core/scripts/y.sh", pr, wd, md) == pr / "core/scripts/y.sh"
    # an absolute path is returned as-is
    ab = tmp_path / "abs.sh"
    assert TICK._resolve_script_path(str(ab), pr, wd, md) == ab
    # FAIL-OPEN: world/ with no configured WORLD_DIR falls back to project_root
    assert TICK._resolve_script_path("world/z.sh", pr, None, None) == pr / "world/z.sh"
    # bare "world"/"meta" (no sub-path) is NOT treated as an external prefix
    assert TICK._resolve_script_path("world", pr, wd, md) == pr / "world"


def test_run_tick_uses_external_world_path(tmp_path):
    """run_tick threads world_dir/meta_dir into resolution: a world/ probe script
    is handed to the runner as an EXTERNAL path, not project_root/world."""
    probe = {**PROBE_P1, "id": "wp", "script": "world/scripts/probe-x.sh"}
    reg = _write_registry(tmp_path, enabled=["wp"], probes=[probe])
    state = tmp_path / "state.json"
    proj = tmp_path / "proj"
    ext_world = tmp_path / "ext-world"
    seen = {}

    # Monkeypatch the module's _paths import target by seeding WORLD_DIR/META_DIR
    # through a fake _paths module in sys.modules for the duration of the tick.
    import sys, types
    fake = types.ModuleType("_paths")
    fake.WORLD_DIR = str(ext_world)
    fake.META_DIR = str(tmp_path / "ext-meta")
    old = sys.modules.get("_paths")
    sys.modules["_paths"] = fake
    try:
        def runner(script_abs, args):
            seen["script"] = str(script_abs)
            return (0, "ok")
        TICK.run_tick(str(reg), str(state), project_root=proj,
                      probe_runner=runner, on_trip_fn=lambda *a: {})
    finally:
        if old is not None:
            sys.modules["_paths"] = old
        else:
            sys.modules.pop("_paths", None)

    # The runner received the EXTERNAL world path, never project_root/world.
    assert seen["script"] == str(ext_world / "scripts/probe-x.sh")
    assert "proj/world" not in seen["script"]


# ---- E2: declarative exit-contract (clean_exit_codes, timeout_is_trip) ------

def test_clean_exit_codes_custom(tmp_path):
    """A probe declaring clean_exit_codes:[0,2] treats rc=2 as clean, not a trip."""
    probe = {**PROBE_P1, "id": "p2", "clean_exit_codes": [0, 2]}
    reg = _write_registry(tmp_path, enabled=["p2"], probes=[probe])
    state = tmp_path / "state.json"
    trips = []
    res = TICK.run_tick(str(reg), str(state),
                        probe_runner=lambda s, a: (2, "exit 2 but declared clean"),
                        on_trip_fn=lambda p, e: trips.append(1) or {})
    assert res["clean"] == ["p2"]
    assert res["tripped"] == []
    assert trips == []          # converter never called
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["probes"]["p2"]["last_outcome"] == "clean"


def test_non_clean_code_still_trips(tmp_path):
    """With clean_exit_codes:[0,2], an rc NOT in the set (3) still trips."""
    probe = {**PROBE_P1, "id": "p2", "clean_exit_codes": [0, 2]}
    reg = _write_registry(tmp_path, enabled=["p2"], probes=[probe])
    state = tmp_path / "state.json"
    trips = []
    res = TICK.run_tick(str(reg), str(state),
                        probe_runner=lambda s, a: (3, "genuine fail"),
                        on_trip_fn=lambda p, e: trips.append(1) or {"filed": True, "goal_id": "g-1"})
    assert res["tripped"] == ["p2"]
    assert len(trips) == 1


def test_timeout_is_logged_error_not_trip(tmp_path):
    """Default (timeout_is_trip=false): rc=124 timeout is a logged error, NO goal."""
    reg = _write_registry(tmp_path, enabled=["p1"], probes=[PROBE_P1])
    state = tmp_path / "state.json"
    trips = []
    res = TICK.run_tick(str(reg), str(state),
                        probe_runner=lambda s, a: (124, "probe timed out after 60s"),
                        on_trip_fn=lambda p, e: trips.append(1) or {})
    assert res["tripped"] == []
    assert res["filed"] == []
    assert trips == []          # converter NEVER called on a timeout
    assert any("timed out" in (e.get("error", "")) for e in res["errors"])
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["probes"]["p1"]["last_outcome"] == "timeout"
    assert st["probes"]["p1"]["last_run"]        # interval gate still advances


def test_timeout_is_trip_when_configured(tmp_path):
    """timeout_is_trip:true -> rc=124 falls through to a normal trip."""
    probe = {**PROBE_P1, "id": "p3", "timeout_is_trip": True}
    reg = _write_registry(tmp_path, enabled=["p3"], probes=[probe])
    state = tmp_path / "state.json"
    trips = []
    res = TICK.run_tick(str(reg), str(state),
                        probe_runner=lambda s, a: (124, "timed out"),
                        on_trip_fn=lambda p, e: trips.append(1) or {"filed": True, "goal_id": "g-9"})
    assert res["tripped"] == ["p3"]
    assert len(trips) == 1
    assert res["filed"] == [{"probe": "p3", "goal_id": "g-9"}]


# ---- E3: subprocess-env parity (real _subprocess_runner) -------------------

def test_subprocess_runner_env_and_cwd(tmp_path, monkeypatch):
    """The REAL runner sets cwd=project_root and guarantees MIND_AGENT in env,
    so a probe sees the same identity + working dir as the loop's Bash path."""
    probe = tmp_path / "echo-env.sh"
    probe.write_text("#!/usr/bin/env bash\necho \"AGENT=$MIND_AGENT\"\npwd -P\nexit 0\n",
                     encoding="utf-8")
    probe.chmod(0o755)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("MIND_AGENT", "alpha")
    rc, out = TICK._subprocess_runner(str(probe), [], project_root=proj)
    assert rc == 0
    assert "AGENT=alpha" in out
    assert os.path.realpath(str(proj)) in out    # ran with cwd=project_root


def test_probe_env_fills_agent_when_absent(tmp_path, monkeypatch):
    """_probe_env guarantees MIND_AGENT even when the ambient env lacks it
    (cron / bare-CLI parity) -- resolved from _paths.AGENT_NAME."""
    monkeypatch.delenv("MIND_AGENT", raising=False)
    import sys, types
    fake = types.ModuleType("_paths")
    fake.AGENT_NAME = "zeta"
    old = sys.modules.get("_paths")
    sys.modules["_paths"] = fake
    try:
        env = TICK._probe_env()
    finally:
        if old is not None:
            sys.modules["_paths"] = old
        else:
            sys.modules.pop("_paths", None)
    assert env.get("MIND_AGENT") == "zeta"


def test_subprocess_runner_timeout_returns_124(tmp_path):
    """A probe that exceeds its timeout returns the rc=124 sentinel + message."""
    probe = tmp_path / "hang.sh"
    probe.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
    probe.chmod(0o755)
    rc, out = TICK._subprocess_runner(str(probe), [], timeout=1)
    assert rc == 124
    assert "timed out" in out


def test_subprocess_runner_missing_script_not_clean(tmp_path):
    """A missing probe script is never classified clean. (`bash missing.sh`
    exits 127; a true launch exception returns None -- both are non-zero, so
    neither is silently swallowed as a clean pass.)"""
    rc, out = TICK._subprocess_runner(str(tmp_path / "nonexistent.sh"), [],
                                      project_root=tmp_path)
    assert rc != 0
