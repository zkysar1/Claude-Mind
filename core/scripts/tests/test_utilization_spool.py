"""Writer half of the utilization-counter split ().

The tests that matter most here are NOT the "does it append a line" ones — they
are the two that pin failures which produce PLAUSIBLE WRONG NUMBERS rather than
errors:

  * test_first_touch_seeds_from_embedded_counters (+ its explicit
    test_without_seeding_other_counters_would_read_as_zero control) — the
    sidecar WINS over the embedded field, so a first-touch entry containing only
    the incremented counter silently zeroes every other counter for that record.
  * test_every_generated_spool_name_is_sync_excluded — a synced counter spool
    would have every box drain every other box's deltas, inflating counters
    fleet-wide with no error anywhere.

Both fail as quiet corruption of advisory statistics that feed retrieval
scoring, scar-tissue review and bulk-retire decisions.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _utilization_store as us  # noqa: E402


def _load_flush():
    spec = importlib.util.spec_from_file_location(
        "uflush_test", _SCRIPTS / "utilization-flush.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["uflush_test"] = mod
    spec.loader.exec_module(mod)
    return mod


uf = _load_flush()


class _Args:
    """Stand-in for argparse.Namespace with flush-relevant defaults."""

    def __init__(self, **kw):
        self.min_interval_seconds = 0
        self.burst_records = 500
        self.force = True
        self.dry_run = False
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")   # guard-955
    return tmp_path


# --- the flag: default-OFF is the whole safety property ---------------------

def test_spooling_is_off_by_default(monkeypatch):
    monkeypatch.delenv(us.SPOOLED_ENV, raising=False)
    assert us.spooled_enabled() is False


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True),
    ("0", False), ("false", False), ("", False), ("maybe", False),
])
def test_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv(us.SPOOLED_ENV, value)
    assert us.spooled_enabled() is expected


def test_flag_name_matches_the_cutover_registry():
    """A rename here that is not mirrored in store-cutover-check.py makes the
    attestation gate prove the wrong thing — silently."""
    src = (_SCRIPTS / "store-cutover-check.py").read_text(encoding="utf-8")
    assert us.SPOOLED_ENV in src


# --- the spool append ------------------------------------------------------

def test_record_increment_writes_one_delta_line(world):
    assert us.record_increment("guardrails", "guard-1", "times_helpful",
                               world_dir=world) is True
    lines = (world / us.spool_name("guardrails")).read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["id"] == "guard-1"
    assert rec["counter"] == "times_helpful"
    assert rec["delta"] == 1
    assert rec["ts"]


def test_record_increment_never_raises_and_reports_failure(world):
    """False is the signal the caller falls back on — it must not be swallowed
    into a silent success."""
    assert us.record_increment("guardrails", "", "times_helpful",
                               world_dir=world) is False
    assert us.record_increment("guardrails", "guard-1", "",
                               world_dir=world) is False
    assert us.record_increment("not-a-kind", "guard-1", "times_helpful",
                               world_dir=world) is False
    assert not (world / us.spool_name("guardrails")).exists()


def test_spool_is_never_admitted_to_the_content_store(world):
    """The strict segment matcher — not the naming convention — is what keeps a
    machine-local buffer out of the shared content store."""
    for kind in us.KINDS:
        seg = us._segment_re(kind)
        assert not seg.match(us.spool_name(kind))
        assert not seg.match(us.flushing_name(kind))
        assert not seg.match(us.counters_name(kind))
        assert seg.match("%s-2026-08-18.jsonl" % kind)      # positive control


def test_spool_files_are_not_returned_by_store_paths(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    (world / "guardrails.jsonl").write_text("")
    us.record_increment("guardrails", "guard-1", "times_helpful", world_dir=world)
    names = [p.name for p in us.store_paths("guardrails", world)]
    assert names == ["guardrails.jsonl"]


# --- sync exclusion --------------------------------------------------------

def test_every_generated_spool_name_is_sync_excluded():
    """Generated names vs the literal exclusion set. If KINDS grows, this fails
    rather than letting a per-box spool sync fleet-wide."""
    import owncloud_sync
    for kind in us.KINDS:
        for name in (us.spool_name(kind), us.flushing_name(kind),
                     us.flush_stamp_name(kind)):
            assert name in owncloud_sync._EXCLUDE_NAMES, name
    # the lock rides the *.lock glob rather than an exact entry
    assert any(us.flush_lock_name(k).endswith(".lock") for k in us.KINDS)


# --- aggregation -----------------------------------------------------------

def test_aggregate_sums_repeated_increments():
    agg = uf.aggregate([
        {"id": "rb-1", "counter": "times_helpful", "delta": 1},
        {"id": "rb-1", "counter": "times_helpful", "delta": 1},
        {"id": "rb-1", "counter": "times_active", "delta": 1},
        {"id": "rb-2", "counter": "times_helpful", "delta": 5},
    ])
    assert agg == {"rb-1": {"times_helpful": 2, "times_active": 1},
                   "rb-2": {"times_helpful": 5}}


def test_aggregate_drops_malformed_without_guessing():
    agg = uf.aggregate([
        {"counter": "times_helpful", "delta": 1},          # no id
        {"id": "rb-1", "delta": 1},                        # no counter
        {"id": "rb-1", "counter": "x", "delta": "many"},   # non-int
        {"id": "rb-1", "counter": "y", "delta": 0},        # no-op
    ])
    assert agg == {}


# --- THE SEEDING CRUX ------------------------------------------------------

def _content(world, kind, records):
    (world / ("%s.jsonl" % kind)).write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_first_touch_seeds_from_embedded_counters(world, monkeypatch):
    """Incrementing ONE counter must not zero the record's other counters."""
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [{
        "id": "guard-1",
        "utilization": {"times_helpful": 41, "retrieval_count": 100,
                        "times_active": 7},
    }])
    us.record_increment("guardrails", "guard-1", "times_active", world_dir=world)
    uf.flush_kind("guardrails", str(world), ["times_helpful", "times_active",
                                             "retrieval_count"], None, _Args())

    counters = us.load_counters("guardrails", world)
    assert counters["guard-1"]["times_active"] == 8      # 7 + 1
    assert counters["guard-1"]["times_helpful"] == 41    # PRESERVED
    assert counters["guard-1"]["retrieval_count"] == 100


def test_without_seeding_other_counters_would_read_as_zero(world):
    """Explicit control for the test above: this is what the bug looks like.

    Not a test of production code — it demonstrates that the sidecar-wins rule
    makes an unseeded first-touch entry destructive, so the seeding test is
    pinning something real rather than restating the implementation.
    """
    rec = {"id": "guard-1", "utilization": {"times_helpful": 41}}
    unseeded_sidecar = {"guard-1": {"times_active": 1}}
    got = us.utilization_of(rec, unseeded_sidecar)
    assert got.get("times_helpful", 0) == 0     # the silent regression
    assert us.utilization_of(rec, {}).get("times_helpful") == 41


def test_second_touch_does_not_reseed_from_stale_embedded(world, monkeypatch):
    """Once the sidecar owns an id, the frozen embedded snapshot is ignored."""
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [{
        "id": "guard-1", "utilization": {"times_active": 7}}])
    names = ["times_active"]
    for _ in range(2):
        us.record_increment("guardrails", "guard-1", "times_active",
                            world_dir=world)
        uf.flush_kind("guardrails", str(world), names, None, _Args())
    # 7 seeded once, then +1 and +1 — not re-seeded to 7 on the second flush.
    assert us.load_counters("guardrails", world)["guard-1"]["times_active"] == 9


def test_unknown_id_seeds_from_defaults_not_from_nothing(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [])
    us.record_increment("guardrails", "guard-new", "times_helpful",
                        world_dir=world)
    uf.flush_kind("guardrails", str(world),
                  ["times_helpful", "times_noise"], None, _Args())
    counters = us.load_counters("guardrails", world)["guard-new"]
    assert counters["times_helpful"] == 1
    assert counters["times_noise"] == 0     # materialised, not absent


# --- drain mechanics -------------------------------------------------------

def test_flush_consumes_the_spool(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [])
    us.record_increment("guardrails", "guard-1", "times_helpful", world_dir=world)
    assert (world / us.spool_name("guardrails")).exists()
    uf.flush_kind("guardrails", str(world), ["times_helpful"], None, _Args())
    assert not (world / us.spool_name("guardrails")).exists()
    assert not (world / us.flushing_name("guardrails")).exists()


def test_crash_residue_drains_before_the_spool_rotates(world, monkeypatch):
    """A .flushing file left by a crash must not be overwritten by the rename."""
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [])
    (world / us.flushing_name("guardrails")).write_text(
        json.dumps({"id": "guard-1", "counter": "times_helpful", "delta": 5}) + "\n")
    us.record_increment("guardrails", "guard-1", "times_helpful", world_dir=world)
    uf.flush_kind("guardrails", str(world), ["times_helpful"], None, _Args())
    # 5 from the residue + 1 from the spool: neither lost.
    assert us.load_counters("guardrails", world)["guard-1"]["times_helpful"] == 6


def test_torn_line_is_skipped_not_fatal(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [])
    spool = world / us.spool_name("guardrails")
    spool.write_text(
        json.dumps({"id": "guard-1", "counter": "times_helpful", "delta": 1})
        + "\n{ this line is torn\n"
        + json.dumps({"id": "guard-2", "counter": "times_helpful", "delta": 1})
        + "\n")
    uf.flush_kind("guardrails", str(world), ["times_helpful"], None, _Args())
    counters = us.load_counters("guardrails", world)
    assert counters["guard-1"]["times_helpful"] == 1
    assert counters["guard-2"]["times_helpful"] == 1


def test_dry_run_changes_nothing(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [])
    us.record_increment("guardrails", "guard-1", "times_helpful", world_dir=world)
    uf.flush_kind("guardrails", str(world), ["times_helpful"], None,
                  _Args(dry_run=True))
    assert (world / us.spool_name("guardrails")).exists()      # untouched
    assert us.load_counters("guardrails", world) == {}


def test_no_spool_is_a_silent_noop(world):
    assert uf.flush_kind("guardrails", str(world), ["times_helpful"], None,
                         _Args()) == 0
    assert not (world / us.counters_name("guardrails")).exists()


def test_interval_gate_defers_a_small_spool(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [])
    us.record_increment("guardrails", "guard-1", "times_helpful", world_dir=world)
    (world / us.flush_stamp_name("guardrails")).write_text(str(9e18))  # "just flushed"
    uf.flush_kind("guardrails", str(world), ["times_helpful"], None,
                  _Args(force=False, min_interval_seconds=300))
    assert (world / us.spool_name("guardrails")).exists()      # deferred
    assert us.load_counters("guardrails", world) == {}


def test_burst_overrides_the_interval_gate(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [])
    for _ in range(6):
        us.record_increment("guardrails", "guard-1", "times_helpful",
                            world_dir=world)
    (world / us.flush_stamp_name("guardrails")).write_text(str(9e18))
    uf.flush_kind("guardrails", str(world), ["times_helpful"], None,
                  _Args(force=False, min_interval_seconds=300, burst_records=5))
    assert us.load_counters("guardrails", world)["guard-1"]["times_helpful"] == 6


# --- score recompute -------------------------------------------------------

def test_recompute_runs_over_the_merged_counters(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [])
    seen = {}

    def _fake_recompute(rec):
        seen["util"] = rec["utilization"]
        rec["utilization"]["utilization_score"] = 0.5

    us.record_increment("guardrails", "guard-1", "times_helpful", world_dir=world)
    uf.flush_kind("guardrails", str(world), ["times_helpful"], _fake_recompute,
                  _Args())
    assert seen["util"]["times_helpful"] == 1
    assert us.load_counters("guardrails", world)["guard-1"]["utilization_score"] == 0.5


def test_a_failing_recompute_does_not_lose_the_increment(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    _content(world, "guardrails", [])

    def _boom(rec):
        raise RuntimeError("schema drift")

    us.record_increment("guardrails", "guard-1", "times_helpful", world_dir=world)
    uf.flush_kind("guardrails", str(world), ["times_helpful"], _boom, _Args())
    assert us.load_counters("guardrails", world)["guard-1"]["times_helpful"] == 1


# --- the endpoint wiring ---------------------------------------------------
#
# WHY THESE EXIST: the spool branch sits inside a try/except that falls through
# to the legacy path on ANY exception, which is the right fail-safe posture and
# also the perfect hiding place for a permanently-inert feature — an unresolvable
# import would make every increment take the legacy path forever while the flag
# reads ON and nothing anywhere errors. The `content store is NOT rewritten`
# assertion below is the one that actually pins the saving.

_REPO_ROOT = _SCRIPTS.parent


def _endpoint():
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    import importlib
    return importlib.import_module("mind_api.src.endpoints.store")


class _FakePaths:
    # Path, not str: agent_paths._absolutize returns Path objects and
    # store_registry builds store paths with the `/` operator, so a str here
    # fails with `unsupported operand type(s) for /` the moment the LEGACY path
    # is exercised — and only then, since the spool branch returns before any
    # path resolution. Measured while writing these tests: the flag-ON cases all
    # passed against a str fake and only the flag-OFF case caught it.
    def __init__(self, world, agent_name="alpha"):
        self.world = Path(world)
        self.meta = Path(world)
        self.agent = Path(world)
        self.project_root = _REPO_ROOT
        self.agent_name = agent_name


class _FakeCtx:
    def __init__(self, world, query):
        self.paths = _FakePaths(world)
        self.query = query
        self.body = b""
        self.headers = {"x-mind-agent": "alpha"}


@pytest.fixture
def guard_store(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    monkeypatch.setenv("MIND_WORLD", str(world))
    _content(world, "guardrails", [{
        "id": "guard-1",
        "rule": "x",
        "utilization": {"times_helpful": 41, "retrieval_count": 100,
                        "times_active": 7},
    }])
    return world


def test_endpoint_import_of_the_spool_module_resolves():
    """Guards the silently-inert case: the daemon must actually be able to
    import the module the spool branch depends on."""
    _endpoint()                       # brings file_locks' sys.path insert with it
    import _utilization_store as reimported
    assert reimported.SPOOLED_ENV == us.SPOOLED_ENV


def test_flag_off_takes_the_legacy_path(guard_store, monkeypatch):
    monkeypatch.delenv(us.SPOOLED_ENV, raising=False)
    store = _endpoint()
    ctx = _FakeCtx(guard_store, {"store": "guardrails", "id": "guard-1",
                                 "field": "utilization.times_active"})
    resp = store.increment(ctx)
    payload = json.loads(resp.body) if isinstance(resp.body, (bytes, str)) else {}
    assert payload.get("spooled") is not True
    assert not (guard_store / us.spool_name("guardrails")).exists()


def test_flag_on_spools_and_does_not_rewrite_the_content_store(guard_store,
                                                               monkeypatch):
    """THE ASSERTION THAT IS THE WHOLE GOAL: with the flag on, the multi-megabyte
    content object is not touched at all."""
    monkeypatch.setenv(us.SPOOLED_ENV, "1")
    content = guard_store / "guardrails.jsonl"
    before = content.read_bytes()

    store = _endpoint()
    ctx = _FakeCtx(guard_store, {"store": "guardrails", "id": "guard-1",
                                 "field": "utilization.times_active"})
    resp = store.increment(ctx)
    payload = json.loads(resp.body) if isinstance(resp.body, (bytes, str)) else {}

    assert payload.get("ok") is True
    assert payload.get("spooled") is True
    assert content.read_bytes() == before          # NOT rewritten
    lines = (guard_store / us.spool_name("guardrails")).read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["counter"] == "times_active"


def test_spooled_increment_survives_the_round_trip(guard_store, monkeypatch):
    """End to end: endpoint -> spool -> flush -> sidecar, with the record's
    other counters intact."""
    monkeypatch.setenv(us.SPOOLED_ENV, "1")
    store = _endpoint()
    ctx = _FakeCtx(guard_store, {"store": "guardrails", "id": "guard-1",
                                 "field": "utilization.times_active"})
    store.increment(ctx)
    store.increment(ctx)
    uf.flush_kind("guardrails", str(guard_store),
                  ["times_helpful", "times_active", "retrieval_count"],
                  None, _Args())
    counters = us.load_counters("guardrails", guard_store)["guard-1"]
    assert counters["times_active"] == 9        # 7 embedded + 2
    assert counters["times_helpful"] == 41      # preserved through the split


def test_non_utilization_increment_is_untouched_by_the_branch(guard_store,
                                                              monkeypatch):
    """The branch is scoped to `utilization.` — any other counter family keeps
    the legacy path even with the flag on."""
    monkeypatch.setenv(us.SPOOLED_ENV, "1")
    store = _endpoint()
    ctx = _FakeCtx(guard_store, {"store": "guardrails", "id": "guard-1",
                                 "field": "nonsense.times_active"})
    resp = store.increment(ctx)
    payload = json.loads(resp.body) if isinstance(resp.body, (bytes, str)) else {}
    assert payload.get("spooled") is not True
    assert not (guard_store / us.spool_name("guardrails")).exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
