""": per-box retrieval-index identity — publish it, census it fleet-wide.

The defect being guarded: a PER-BOX DERIVED CACHE whose correctness depends on a
SHARED config value has no parity check by construction. Config-parity tooling
sees an identical shared half everywhere; no shared store sees the local half.
Measured 2026-09-03 — a box served retrieval from a bge-small index against
MiniLM-calibrated cosine floors for five weeks with every liveness probe green.

Two legs, and the tests pin BOTH plus their asymmetry (guard-1414: fixing either
alone still shows nothing, and each is individually verifiable, so a one-leg
close can honestly report success while the reader stays blind):

  DATA  — every box publishes, from ABOVE heartbeat-tick's agent-state gate.
  SCOPE — one box reads every agent row, from the AUTHORITATIVE store.

Daemon-safe: pure path/dict arithmetic over a tmp world. No daemon_integration.

Run:
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_retrieval_index_status.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ris = _load("retrieval_index_status_under_test", "retrieval_index_status.py")

CONFIGURED = "all-MiniLM-L6-v2"
DRIFTED_MODEL = "BAAI/bge-small-en-v1.5"   # the model in the real incident


def _write_index(tmp_path: Path, model, count=100) -> Path:
    d = tmp_path / "idx"
    d.mkdir(parents=True, exist_ok=True)
    payload = {"backend": "fastembed", "dim": 384, "count": count}
    if model is not None:
        payload["model"] = model
    (d / "meta.json").write_text(json.dumps(payload), encoding="utf-8")
    return d


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S")


# --------------------------- local_block (DATA leg) ---------------------------

def test_matching_model_is_not_drifted(tmp_path, monkeypatch):
    monkeypatch.setattr(ris, "configured_model", lambda: CONFIGURED)
    monkeypatch.setattr(ris, "channel_status", lambda: "alive")
    b = ris.local_block(_write_index(tmp_path, CONFIGURED))
    assert b["drifted"] is False
    assert b["model"] == CONFIGURED and b["configured_model"] == CONFIGURED
    assert b["doc_count"] == 100
    assert b["built_at"] and b["published_at"] and b["box"]


def test_mismatched_model_is_drifted(tmp_path, monkeypatch):
    """The whole point. Without this the census reports 'ok' forever."""
    monkeypatch.setattr(ris, "configured_model", lambda: CONFIGURED)
    monkeypatch.setattr(ris, "channel_status", lambda: "alive")
    b = ris.local_block(_write_index(tmp_path, DRIFTED_MODEL))
    assert b["drifted"] is True
    assert ris.classify(b) == ris.VERDICT_DRIFTED


def test_absent_index_is_a_channel_fault_not_a_drift_claim(tmp_path, monkeypatch):
    """An unknown model must NOT be reported as drift: asserting a mismatch from
    a missing file manufactures a finding out of no evidence. The condition is
    still surfaced — under the name that is actually true (the channel)."""
    monkeypatch.setattr(ris, "configured_model", lambda: CONFIGURED)
    monkeypatch.setattr(ris, "channel_status", lambda: "DEAD: flags ON but no index")
    b = ris.local_block(tmp_path / "does-not-exist")
    assert b["model"] is None
    assert b["drifted"] is False, "missing index must not masquerade as drift"
    assert ris.classify(b) == ris.VERDICT_CHANNEL


def test_doc_count_falls_back_to_len_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(ris, "configured_model", lambda: CONFIGURED)
    monkeypatch.setattr(ris, "channel_status", lambda: "alive")
    d = tmp_path / "idx"
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps({"model": CONFIGURED, "docs": [{"id": "a"}, {"id": "b"}]}),
        encoding="utf-8")
    assert ris.local_block(d)["doc_count"] == 2


# ------------------------------- classify ------------------------------------

def test_classify_missing_for_empty_or_nondict():
    for v in (None, {}, [], "nope"):
        assert ris.classify(v) == ris.VERDICT_MISSING


def test_drift_outranks_a_bad_channel():
    """Order is load-bearing: a drifted index answers queries WRONG, a dead
    channel merely answers them token-only. Reporting the quiet failure would
    launder the loud one."""
    both = {"drifted": True, "channel": "DEAD: no index"}
    assert ris.classify(both) == ris.VERDICT_DRIFTED


def test_classify_ok_requires_alive_channel():
    assert ris.classify({"drifted": False, "channel": "alive"}) == ris.VERDICT_OK
    assert ris.classify({"drifted": False, "channel": "off"}) == ris.VERDICT_CHANNEL


# -------------------------------- liveness -----------------------------------

def test_is_live_window():
    assert ris._is_live({"last_active": _iso(0.1)}) is True
    assert ris._is_live({"last_active": _iso(ris.LIVE_THRESHOLD_HOURS + 1)}) is False


def test_unparseable_or_absent_last_active_reads_not_live():
    """Fails toward NOT-live on purpose: this predicate only de-escalates a
    report, so failing this way can hide nothing the census does not still
    print — while failing the other way resurrects dead test rows into a
    permanent alert."""
    for row in ({}, {"last_active": None}, {"last_active": "not-a-date"}, "nope"):
        assert ris._is_live(row) is False


# ---------------------------- census (SCOPE leg) ------------------------------

class _FakeTeamState:
    """Stands in for _team_state so the census can be driven over a known fleet.

    Mirrors the REAL return shape, which is the point: provenance is
    {"by_agent": {...}, "roster": ...}, NOT a flat agent->layer map. Reading it
    flat returns None for every agent and renders a fully-authoritative read as
    "we could not tell" — the guard-1753 signal inverted into silence. That was
    a live defect in this module, caught only by reading the real shape.
    """

    def __init__(self, rows, rows_path):
        self._rows = rows
        self._rows_path = rows_path

    def load_rows_authoritative_with_provenance(self, world_dir):
        return self._rows, {"by_agent": {a: "authoritative" for a in self._rows},
                            "roster": "authoritative"}

    def rows_dir(self, world_dir):
        return self._rows_path


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    rows_path = tmp_path / "team-state" / "agents"
    rows_path.mkdir(parents=True)
    rows = {
        "alpha": {"last_active": _iso(0.1), "retrieval_index": {
            "box": "cc-09", "model": CONFIGURED, "configured_model": CONFIGURED,
            "drifted": False, "channel": "alive", "doc_count": 16874}},
        "bravo": {"last_active": _iso(0.1), "retrieval_index": {
            "box": "cc-05", "model": DRIFTED_MODEL, "configured_model": CONFIGURED,
            "drifted": True, "channel": "alive", "doc_count": 7633}},
        "echo": {"last_active": _iso(0.1)},                       # live, no block
        "zz-hbtest": {"last_active": _iso(900)},                  # residue
    }
    for a in rows:
        (rows_path / f"{a}.yaml").write_text("{}\n", encoding="utf-8")
    fake = _FakeTeamState(rows, rows_path)
    monkeypatch.setitem(sys.modules, "_team_state", fake)
    monkeypatch.setattr(ris, "configured_model", lambda: CONFIGURED)
    return rows


def test_census_iterates_every_agent_row_not_one(fleet, tmp_path):
    """The goal's own check: 'assert count == number of agent shards, not 1'.
    A reader that answers for the bound agent only is the fleet-visibility
    defect this goal exists to close, wearing a passing test."""
    rep = ris.census(tmp_path)
    assert rep["agent_count"] == 4
    assert rep["shard_count"] == 4
    assert rep["agent_count"] == rep["shard_count"]
    assert set(rep["agents"]) == set(fleet)


def test_census_reports_a_drifted_PEER_from_this_box(fleet, tmp_path):
    """Outcome 3: a drifted index on ONE box is reported from a DIFFERENT box.
    bravo's block says box=cc-05; the census runs here and still names it."""
    rep = ris.census(tmp_path)
    assert rep["drifted"] == ["bravo"]
    assert rep["agents"]["bravo"]["block"]["box"] == "cc-05"
    assert rep["agents"]["alpha"]["verdict"] == ris.VERDICT_OK


def test_census_reads_provenance_from_the_by_agent_submap(fleet, tmp_path):
    """Regression pin for a real defect: provenance was read as prov.get(agent)
    against a {"by_agent": {...}} shape, so 15/15 agents reported None while the
    read was in fact fully authoritative."""
    rep = ris.census(tmp_path)
    assert rep["roster_provenance"] == "authoritative"
    assert {i["provenance"] for i in rep["agents"].values()} == {"authoritative"}


def test_non_live_rows_are_de_escalated_but_never_hidden(fleet, tmp_path):
    """Residue rows stay in the per-agent map and stay out of the alert lists."""
    rep = ris.census(tmp_path)
    assert "zz-hbtest" in rep["agents"], "a row must never vanish from the census"
    assert "zz-hbtest" not in rep["missing"]
    assert rep["live_count"] == 3 and rep["agent_count"] == 4
    assert rep["missing"] == ["echo"], "only LIVE agents count as gaps"


def test_census_survives_a_row_read_failure(tmp_path, monkeypatch):
    class _Boom:
        def load_rows_authoritative_with_provenance(self, world_dir):
            raise RuntimeError("s3 unavailable")

        def rows_dir(self, world_dir):
            return tmp_path
    monkeypatch.setitem(sys.modules, "_team_state", _Boom())
    rep = ris.census(tmp_path)
    assert rep["error"] and rep["agent_count"] == 0


# ---------------------- wiring pins (guard-1943 / guard-3448) -----------------

HEARTBEAT = CORE_SCRIPTS / "heartbeat-tick.sh"
WATCHDOG = CORE_SCRIPTS / "agent-watchdog.py"


def test_publish_is_wired_into_heartbeat_tick():
    """guard-1943: pinning the writer says nothing about the wiring. 
    fixed heartbeat-tick's ordering correctly and stayed inert for months
    because nothing called it, with its own tests green throughout."""
    src = HEARTBEAT.read_text(encoding="utf-8")
    assert "retrieval_index_status.py" in src
    assert "agent_status.$MIND_AGENT.retrieval_index" in src


def test_publish_sits_ABOVE_the_agent_state_gate():
    """THE load-bearing placement. A cross-box worker Body is IDLE by design, so
    heartbeat-tick exits 2 at the state gate — a publish below it would never run
    on most of the fleet, which is exactly the population this census exists to
    make visible. heartbeat-tick's own comment names copying the below-gate
    agent-wide write as 'the trap here, not the model'."""
    lines = HEARTBEAT.read_text(encoding="utf-8").splitlines()
    publish = next(i for i, l in enumerate(lines)
                   if "agent_status.$MIND_AGENT.retrieval_index" in l)
    gate = next(i for i, l in enumerate(lines) if l.startswith("# --- State gate"))
    assert publish < gate, (
        "the retrieval_index publish moved BELOW the agent-state gate; it will "
        "no longer run on any worker Body (IDLE by design) and the fleet census "
        "will silently answer for the reducer alone")


def test_publish_uses_python3_not_the_windows_only_launcher():
    """heartbeat-tick.sh sources _paths.sh, which is the condition
    python-invocation.md names for python3. `py` is a Windows launcher and dies
    rc=127 on Linux — and the call site swallows stderr, so that failure would be
    a publish that never happens and never says so."""
    src = HEARTBEAT.read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if "retrieval_index_status.py" in l
                and "publish-value" in l)
    assert "python3 " in line and "py -3" not in line


def test_probe_is_registered_for_the_reducer_and_filtered_off_workers():
    """The DATA/SCOPE asymmetry, pinned in both directions. Publishing happens on
    every box; REPORTING happens on one, because N Bodies polling one fleet
    condition means N alerts for one fault (the InfraComponentProbe precedent).
    Asserting only the positive would let a change that adds it to
    WORKER_SAFE_PROBES stay green while re-creating the duplicate-alert defect."""
    aw = _load("agent_watchdog_under_test", "agent-watchdog.py")
    assert "retrieval-index" not in aw.WORKER_SAFE_PROBES
    ctx_r = aw.WatchdogContext(agent_name="alpha", agent_dir=Path("agents/alpha"),
                               project_root_path=Path("."), body_role="reducer")
    ctx_w = aw.WatchdogContext(agent_name="alpha", agent_dir=Path("agents/alpha"),
                               project_root_path=Path("."), body_role="worker")
    assert "retrieval-index" in [p.name for p in aw.build_probes(ctx_r)]
    assert "retrieval-index" not in [p.name for p in aw.build_probes(ctx_w)]


def test_probe_dedups_and_clears(monkeypatch):
    aw = _load("agent_watchdog_under_test2", "agent-watchdog.py")
    ctx = aw.WatchdogContext(agent_name="alpha", agent_dir=Path("agents/alpha"),
                             project_root_path=Path("."), body_role="reducer")
    probe = aw.RetrievalIndexProbe(ctx)
    reports = [
        {"drifted": ["bravo"], "agents": {"bravo": {"block": {"box": "cc-05", "model": DRIFTED_MODEL}}},
         "configured_model": CONFIGURED},
        {"drifted": ["bravo"], "agents": {"bravo": {"block": {"box": "cc-05", "model": DRIFTED_MODEL}}},
         "configured_model": CONFIGURED},
        {"drifted": [], "agents": {}, "configured_model": CONFIGURED},
    ]
    stub = type(sys)("retrieval_index_status")
    stub.census = lambda *a, **k: reports[stub.i]
    stub.i = 0
    monkeypatch.setitem(sys.modules, "retrieval_index_status", stub)

    ev = probe.check()
    assert len(ev) == 1 and ev[0].event == "retrieval_index_drift"
    assert ev[0].severity == "critical"

    stub.i, probe.last_polled = 1, None      # same drift set, interval elapsed
    assert probe.check() == [], "an unchanged drifted set must not re-alert"

    stub.i, probe.last_polled = 2, None
    ev = probe.check()
    assert len(ev) == 1 and ev[0].event == "retrieval_index_drift_cleared"


def test_probe_is_paced_between_polls(monkeypatch):
    """census() reads every peer shard from the authoritative store (2.7s
    measured for 15 shards). The tick runs every iteration; an unpaced census
    would pay that forever to restate a value that changes when someone rebuilds
    an index."""
    aw = _load("agent_watchdog_under_test3", "agent-watchdog.py")
    ctx = aw.WatchdogContext(agent_name="alpha", agent_dir=Path("agents/alpha"),
                             project_root_path=Path("."), body_role="reducer")
    probe = aw.RetrievalIndexProbe(ctx)
    calls = {"n": 0}

    def _census(*a, **k):
        calls["n"] += 1
        return {"drifted": [], "agents": {}, "configured_model": CONFIGURED}
    stub = type(sys)("retrieval_index_status")
    stub.census = _census
    monkeypatch.setitem(sys.modules, "retrieval_index_status", stub)

    probe.check()
    probe.check()
    probe.check()
    assert calls["n"] == 1, "probe must not re-census inside its interval"


def test_probe_never_raises_when_the_census_explodes(monkeypatch):
    aw = _load("agent_watchdog_under_test4", "agent-watchdog.py")
    ctx = aw.WatchdogContext(agent_name="alpha", agent_dir=Path("agents/alpha"),
                             project_root_path=Path("."), body_role="reducer")
    probe = aw.RetrievalIndexProbe(ctx)
    stub = type(sys)("retrieval_index_status")

    def _boom(*a, **k):
        raise RuntimeError("census exploded")
    stub.census = _boom
    monkeypatch.setitem(sys.modules, "retrieval_index_status", stub)
    assert probe.check() == [], "an advisory probe must degrade, never crash the tick"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
