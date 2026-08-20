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


def test_flusher_has_a_caller_in_the_daemon_sync_loop():
    """utilization-flush.py shipped 2026-08-18 with ZERO callers — increments
    spooled locally and never reached the shared sidecar, found only when the
    2026-08-19 flip-adoption measurement went looking for sidecar PUTs (the
    write half of the rb-8458 adoption gap). This pin asserts the invocation
    in the daemon's own-cloud sync loop stays wired: a capability with no
    caller is indistinguishable from one that was never built."""
    main_py = Path(__file__).resolve().parents[3] / "mind_api" / "src" / "__main__.py"
    src = main_py.read_text(encoding="utf-8")
    sync_thread_body = src.split("def _start_owncloud_sync_thread", 1)[-1]
    assert "utilization-flush.py" in sync_thread_body, (
        "mind_api/src/__main__.py sync thread no longer invokes "
        "utilization-flush.py — the counter spools have no drain again (g-358-05)")
    assert "subprocess.run" in sync_thread_body, (
        "the utilization-flush reference exists in the sync thread but no "
        "subprocess.run invocation found — a comment is not a caller")


# --- the retrieve bump branch () -----------------------------------
#
# retrieve.py's `_locked_bump_jsonl` was the residual churn driver AFTER the
#  endpoint flip: one full-store RMW (history snapshot + fenced PUT +
# changelog row) per store per retrieval call, just to +1 retrieval_count on
# the matched records. These tests pin the spool-routing of that bump — same
# flag, same spool, same fall-back-on-False contract as the endpoint branch
# above.


def _load_retrieve():
    """Load retrieve.py once, with the same env guard as
    test_retrieve_write_locking.py. `_locked_bump_jsonl` takes its path
    explicitly, so the module-level RB_PATH/GUARD_PATH bindings are unused."""
    if "retrieve_spool_test" in sys.modules:
        return sys.modules["retrieve_spool_test"]
    import tempfile
    orig_world = os.environ.get("MIND_WORLD")
    orig_agent = os.environ.get("MIND_AGENT")
    os.environ["MIND_WORLD"] = tempfile.mkdtemp(prefix="retrieve-spool-test-")
    os.environ.pop("MIND_AGENT", None)
    try:
        spec = importlib.util.spec_from_file_location(
            "retrieve_spool_test", _SCRIPTS / "retrieve.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["retrieve_spool_test"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if orig_world is not None:
            os.environ["MIND_WORLD"] = orig_world
        else:
            os.environ.pop("MIND_WORLD", None)
        if orig_agent is not None:
            os.environ["MIND_AGENT"] = orig_agent


@pytest.fixture
def bump_store(world, monkeypatch):
    monkeypatch.setattr(us, "_backend_names", lambda base: None)
    monkeypatch.setenv("MIND_WORLD", str(world))
    _content(world, "guardrails", [
        {"id": "guard-1", "rule": "x", "status": "active",
         "utilization": {"times_helpful": 41, "retrieval_count": 100}},
        {"id": "guard-2", "rule": "y", "status": "active",
         "utilization": {"retrieval_count": 5}},
    ])
    return world


def _bump(world, monkeypatch=None, ids=("guard-1", "guard-2")):
    rmod = _load_retrieve()
    wanted = set(ids)
    return rmod._locked_bump_jsonl(
        world / "guardrails.jsonl",
        lambda rec: rec.get("id") in wanted and rec.get("status") == "active",
        kind="guardrails")


def test_retrieve_bump_flag_off_takes_the_legacy_rmw(bump_store, monkeypatch):
    monkeypatch.delenv(us.SPOOLED_ENV, raising=False)
    _bump(bump_store)
    recs = {json.loads(l)["id"]: json.loads(l) for l in
            (bump_store / "guardrails.jsonl").read_text().splitlines()}
    assert recs["guard-1"]["utilization"]["retrieval_count"] == 101
    assert "last_retrieved" in recs["guard-1"]["utilization"]
    assert not (bump_store / us.spool_name("guardrails")).exists()


def test_retrieve_bump_flag_on_spools_and_does_not_rewrite_the_store(
        bump_store, monkeypatch):
    """THE ASSERTION THAT IS THE GOAL: with the flag on, one retrieval call
    writes zero bytes to the multi-megabyte content store."""
    monkeypatch.setenv(us.SPOOLED_ENV, "1")
    content = bump_store / "guardrails.jsonl"
    before = content.read_bytes()
    returned = _bump(bump_store)
    assert content.read_bytes() == before          # NOT rewritten
    lines = [json.loads(l) for l in
             (bump_store / us.spool_name("guardrails")).read_text().splitlines()]
    assert {(l["id"], l["counter"], l["delta"]) for l in lines} == {
        ("guard-1", "retrieval_count", 1), ("guard-2", "retrieval_count", 1)}
    assert all(len(l.get("ts", "")) >= 10 for l in lines)
    assert {r["id"] for r in returned} == {"guard-1", "guard-2"}


def test_retrieve_bump_kind_none_keeps_the_legacy_path(bump_store, monkeypatch):
    """Control: pattern-signatures / experience call without `kind` — the flag
    alone must not divert them (they have no spool kind to drain into)."""
    monkeypatch.setenv(us.SPOOLED_ENV, "1")
    rmod = _load_retrieve()
    rmod._locked_bump_jsonl(
        bump_store / "guardrails.jsonl",
        lambda rec: rec.get("id") == "guard-1")
    recs = {json.loads(l)["id"]: json.loads(l) for l in
            (bump_store / "guardrails.jsonl").read_text().splitlines()}
    assert recs["guard-1"]["utilization"]["retrieval_count"] == 101
    assert not (bump_store / us.spool_name("guardrails")).exists()


def test_retrieve_bump_only_matched_records_spool(bump_store, monkeypatch):
    monkeypatch.setenv(us.SPOOLED_ENV, "1")
    _bump(bump_store, ids=("guard-2",))
    lines = [json.loads(l) for l in
             (bump_store / us.spool_name("guardrails")).read_text().splitlines()]
    assert [l["id"] for l in lines] == ["guard-2"]


def test_retrieve_bump_failed_spool_falls_back_for_only_the_failed_id(
        bump_store, monkeypatch):
    """A False from record_increment must neither lose that counter NOR
    double-count the ids that DID spool: the legacy RMW runs narrowed."""
    monkeypatch.setenv(us.SPOOLED_ENV, "1")
    real = us.record_increment
    monkeypatch.setattr(
        us, "record_increment",
        lambda kind, rid, counter, **kw: False if rid == "guard-2"
        else real(kind, rid, counter, **kw))
    _bump(bump_store)
    recs = {json.loads(l)["id"]: json.loads(l) for l in
            (bump_store / "guardrails.jsonl").read_text().splitlines()}
    assert recs["guard-2"]["utilization"]["retrieval_count"] == 6   # legacy
    assert recs["guard-1"]["utilization"]["retrieval_count"] == 100  # spooled, untouched
    lines = [json.loads(l) for l in
             (bump_store / us.spool_name("guardrails")).read_text().splitlines()]
    assert [l["id"] for l in lines] == ["guard-1"]


def test_retrieve_bump_round_trip_stamps_last_retrieved(bump_store, monkeypatch):
    """End to end: bump -> spool -> flush -> sidecar. retrieval_count sums on
    top of the seeded embedded value AND last_retrieved advances — without the
    flusher stamp, the sidecar (which wins WHOLESALE in utilization_of) would
    freeze last_retrieved at seed time and actively-retrieved records would
    read as retrieval-idle to retirement sweeps."""
    monkeypatch.setenv(us.SPOOLED_ENV, "1")
    _bump(bump_store, ids=("guard-1",))
    _bump(bump_store, ids=("guard-1",))
    uf.flush_kind("guardrails", str(bump_store),
                  ["times_helpful", "retrieval_count"], None, _Args())
    counters = us.load_counters("guardrails", bump_store)["guard-1"]
    assert counters["retrieval_count"] == 102   # 100 embedded + 2
    assert counters["times_helpful"] == 41      # preserved through the split
    import datetime as _dt
    assert counters["last_retrieved"] == _dt.date.today().isoformat()


# --- last_retrieved stamping (flusher half of ) ---------------------


def test_latest_retrieval_ts_maxes_and_filters():
    got = uf.latest_retrieval_ts([
        {"id": "a", "counter": "retrieval_count", "delta": 1,
         "ts": "2026-08-19T09:00:00"},
        {"id": "a", "counter": "retrieval_count", "delta": 1,
         "ts": "2026-08-20T07:00:00"},
        {"id": "a", "counter": "times_helpful", "delta": 1,
         "ts": "2026-08-21T00:00:00"},          # wrong counter — ignored
        {"id": "b", "counter": "retrieval_count", "delta": 1},   # no ts
        {"id": "b", "counter": "retrieval_count", "delta": 1, "ts": 12345},
    ])
    assert got == {"a": "2026-08-20T07:00:00"}


def test_apply_deltas_stamps_last_retrieved_forward_only():
    merged = uf.apply_deltas(
        {"a": {"retrieval_count": 3, "last_retrieved": "2026-01-01"},
         "b": {"retrieval_count": 1, "last_retrieved": "2026-12-31"}},
        {"a": {"retrieval_count": 1}, "b": {"retrieval_count": 1}},
        {}, ["retrieval_count"],
        last_ts={"a": "2026-08-20T07:00:00", "b": "2026-08-20T07:00:00"})
    assert merged["a"]["last_retrieved"] == "2026-08-20"   # advanced
    assert merged["b"]["last_retrieved"] == "2026-12-31"   # never regressed


def test_without_the_stamp_last_retrieved_would_freeze(bump_store, monkeypatch):
    """Forced-failure control (guard-3534): prove the round-trip assertion can
    go red. Flushing WITHOUT the last_ts wiring leaves last_retrieved absent
    from the sidecar entry — the exact freeze the stamp exists to prevent."""
    monkeypatch.setenv(us.SPOOLED_ENV, "1")
    _bump(bump_store, ids=("guard-1",))
    flushing = bump_store / (us.spool_name("guardrails") + ".flushing")
    (bump_store / us.spool_name("guardrails")).rename(flushing)
    deltas, _ = uf._parse_lossy(flushing)
    merged = uf.apply_deltas(
        {}, uf.aggregate(deltas),
        {"guard-1": {"times_helpful": 41, "retrieval_count": 100}},
        ["times_helpful", "retrieval_count"])   # no last_ts — pre-fix shape
    assert "last_retrieved" not in merged["guard-1"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
