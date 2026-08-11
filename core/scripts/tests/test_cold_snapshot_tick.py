"""Tests for cold-snapshot-tick.py () — the fleet-wide cadence tick
that replaced recurring goal g-115-4317.

Hermetic: no S3, no daemon, no real snapshot. `decide()` is a pure function of
(marker age, marker body, interval, stuck ceiling), so the cadence contract is
tested directly; the backend-touching paths are covered through a stub backend.

The property that matters is NOT "does it fire weekly" — it is "does exactly ONE
box fire per interval". That is carried entirely by the shared marker: a claim
write makes every other box read `fresh` and stand down. So the tests that earn
their keep are the ones pinning what happens when the marker says something
unexpected — absent, mid-run, stuck — because those are the states where a naive
implementation either fires 5x or silently stops firing at all.
"""
import datetime as dt
import importlib.util
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent


def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TICK = _load("cold_snapshot_tick", "cold-snapshot-tick.py")

WEEK = 168 * 3600.0
STUCK = 2 * 3600.0


# ---- decide(): the cadence contract ----------------------------------------

def test_absent_marker_is_due():
    """First run ever, on any box: nothing has claimed the interval."""
    v = TICK.decide(None, None, WEEK, STUCK)
    assert v["due"] is True
    assert v["reason"] == "no-marker"
    assert v["stuck"] is False


def test_elapsed_interval_is_due():
    v = TICK.decide(WEEK + 60, {"status": "ok"}, WEEK, STUCK)
    assert v["due"] is True
    assert v["reason"].startswith("interval-elapsed")
    assert v["stuck"] is False


def test_fresh_marker_is_not_due():
    """The whole point: a partner box already claimed this interval."""
    v = TICK.decide(3600.0, {"status": "ok"}, WEEK, STUCK)
    assert v["due"] is False
    assert v["reason"].startswith("fresh")


def test_boundary_exactly_at_interval_is_due():
    """>= not >: at exactly one interval the next snapshot is owed."""
    assert TICK.decide(WEEK, {"status": "ok"}, WEEK, STUCK)["due"] is True


def test_in_progress_run_does_not_double_fire():
    """A claim younger than the stuck ceiling is a RUNNING peer, not a corpse.

    This is the case that makes claim-before-run safe: the marker is written
    seconds before a multi-minute snapshot, and every other box must read it as
    a live claim rather than as an interval that never completed.
    """
    v = TICK.decide(120.0, {"status": "running"}, WEEK, STUCK)
    assert v["due"] is False
    assert v["stuck"] is False


def test_stuck_claim_refires_and_reports_stuck():
    """A claim still `running` past the ceiling means that run died.

    Without this branch, claiming first would silently skip an entire interval
    whenever a run crashed after the claim — a backup that stops running without
    saying so, which is the exact failure the lane exists to prevent.
    """
    v = TICK.decide(STUCK + 1, {"status": "running"}, WEEK, STUCK)
    assert v["due"] is True
    assert v["stuck"] is True
    assert v["reason"].startswith("prior-run-stuck")


def test_stuck_only_applies_to_running_status():
    """A completed run is never `stuck`, however old it is (within interval)."""
    v = TICK.decide(STUCK + 1, {"status": "ok"}, WEEK, STUCK)
    assert v["due"] is False
    assert v["stuck"] is False


def test_unreadable_body_falls_back_to_time_only():
    """An empty/unparseable body must not resurrect a claimed interval.

    `_read_marker` maps a bad body to {}, so the decision degrades to pure
    age-vs-interval — the claim write still protects the fleet from double-firing.
    """
    assert TICK.decide(3600.0, {}, WEEK, STUCK)["due"] is False
    assert TICK.decide(WEEK + 1, {}, WEEK, STUCK)["due"] is True


# ---- marker key + backend gating -------------------------------------------

class _StubBackend:
    env_id = "test-env"
    bucket = "test-bucket"
    s3 = object()

    def _customer_prefix(self):
        return "cust/"


def test_marker_key_is_one_fixed_key_under_the_env_prefix():
    """One fixed key, beside the snapshots, inside the env root.

    Fixed because ListBucket is DENIED to this principal: the cadence cannot be
    derived by enumerating snapshot keys, so it must be readable at a key known
    without a listing.
    """
    key = TICK._marker_key(_StubBackend(), "cold-snapshots")
    assert key == "cust/test-env/cold-snapshots/_last-run.json"


def test_local_backend_is_a_silent_no_op(monkeypatch, capsys):
    """No object store means no remote retention clock to protect against.

    Same call cold_snapshot.py makes with its `skipped-local-backend` verdict —
    and load-bearing for every test box, which pins STORAGE_BACKEND=local.
    """
    monkeypatch.setattr(TICK, "_backend", lambda: None)
    args = TICK.argparse.Namespace(prefix="cold-snapshots", dry_run=False, run=False)
    assert TICK.do_tick(args) == 0
    assert capsys.readouterr().out == ""


def test_tick_never_raises_into_its_caller(monkeypatch):
    """Fail-open is the contract: this runs inside productivity-check.

    A cadence tick that aborts the close phase would cost far more than a missed
    snapshot, so every error path returns 0.
    """
    def _boom():
        raise RuntimeError("backend exploded")
    monkeypatch.setattr(TICK, "_backend", _boom)
    monkeypatch.setattr(TICK.sys, "argv", ["cold-snapshot-tick.py"])
    assert TICK.main() == 0


# ---- interval override ------------------------------------------------------

def test_interval_env_override(monkeypatch):
    monkeypatch.setenv("COLD_SNAPSHOT_INTERVAL_HOURS", "24")
    assert TICK._hours_env("COLD_SNAPSHOT_INTERVAL_HOURS", 168.0) == 24.0


def test_interval_override_rejects_garbage_and_zero(monkeypatch):
    """A malformed override must not disable the cadence or fire it every tick."""
    monkeypatch.setenv("COLD_SNAPSHOT_INTERVAL_HOURS", "not-a-number")
    assert TICK._hours_env("COLD_SNAPSHOT_INTERVAL_HOURS", 168.0) == 168.0
    monkeypatch.setenv("COLD_SNAPSHOT_INTERVAL_HOURS", "0")
    assert TICK._hours_env("COLD_SNAPSHOT_INTERVAL_HOURS", 168.0) == 168.0


# ---- dedup gate: fail direction ---------------------------------------------

def test_dedup_gate_fails_closed_when_no_queue_is_readable(monkeypatch):
    """An ABSENT queue file must suppress, not permit.

    A missing path raises nothing, so an exception-only net leaves this case
    failing OPEN while the docstring promises CLOSED. Under own-cloud the local
    tree is a read-through cache (guard-980), so "not on this box" is an
    ordinary state, not a fault — the miss is reachable. Found by the g-115-5279
    fresh-eyes pass, which pointed both queues at nonexistent paths and got
    False back.
    """
    monkeypatch.setattr(TICK, "WORLD_DIR", "/nonexistent-world-xyz")
    monkeypatch.setattr(TICK, "AGENT_DIR", "/nonexistent-agent-xyz")
    assert TICK._recent_investigate_exists() is True


def test_dedup_gate_fails_closed_when_paths_unresolvable(monkeypatch):
    monkeypatch.setattr(TICK, "WORLD_DIR", None)
    monkeypatch.setattr(TICK, "AGENT_DIR", None)
    assert TICK._recent_investigate_exists() is True


def test_dedup_gate_permits_when_a_queue_was_actually_read(tmp_path, monkeypatch):
    """The positive control: a readable queue with no match must return False.

    Without this, the fix above could be satisfied by returning True always —
    which would silence the Investigate the lane exists to raise.
    """
    (tmp_path / "aspirations.jsonl").write_text(
        json.dumps({"id": "asp-999", "goals": [
            {"id": "g-999-01", "origin_signal": "idea:unrelated", "status": "pending"}]}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(TICK, "WORLD_DIR", str(tmp_path))
    monkeypatch.setattr(TICK, "AGENT_DIR", None)
    assert TICK._recent_investigate_exists() is False


def test_dedup_gate_suppresses_on_open_duplicate(tmp_path, monkeypatch):
    (tmp_path / "aspirations.jsonl").write_text(
        json.dumps({"id": "asp-999", "goals": [
            {"id": "g-999-02", "origin_signal": TICK.ORIGIN_SIGNAL,
             "status": "pending"}]}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(TICK, "WORLD_DIR", str(tmp_path))
    monkeypatch.setattr(TICK, "AGENT_DIR", None)
    assert TICK._recent_investigate_exists() is True


# ---- wiring: the trigger half of the integration path -----------------------

def test_tick_is_wired_into_iteration_close():
    """The tick must actually be invoked from the close phase.

    Every test above proves the handler behaves; none proves it ever RUNS. That
    gap is this framework's most expensive recurring defect — pre-edit-context-gate.sh
    was 100% inert for 59 days while hand-testing green, and was declared fixed
    twice while still inert. A unit-tested script nobody calls is indistinguishable
    from no script at all, and it fails silently and upward.

    Asserted here rather than as a /verify-learning grep because a suite-collected
    test has strictly more power and runs on every full-suite invocation.
    """
    src = (SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    assert "cold-snapshot-tick.py" in src, (
        "cold-snapshot-tick.py is no longer invoked from iteration-close.sh — "
        "the weekly retention-immune backup has no trigger (g-115-5279)")
    line = next(ln for ln in src.splitlines() if "cold-snapshot-tick.py" in ln
                and not ln.lstrip().startswith("#"))
    assert "--tick" in line or "--tick" in src, "invoked without --tick"


def test_tick_invocation_is_fail_open():
    """The tick must never abort the close phase it runs inside.

    Its neighbours all end in `|| true` for the same reason: a telemetry or
    backup miss is cheap, and a close phase that aborts is not.
    """
    src = (SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    idx = src.index("cold-snapshot-tick.py")
    window = src[idx:idx + 300]
    assert "|| true" in window, (
        "the cold-snapshot tick invocation lost its `|| true` — a failing tick "
        "would now abort productivity-check")
    assert "iteration-close-stderr.log" in window, (
        "tick stderr is no longer routed to the diagnostic log")


# ---- read path --------------------------------------------------------------

def test_read_marker_absent_returns_none(monkeypatch):
    """A 404 is 'never claimed', not an error — it must not abort the tick."""
    from botocore.exceptions import ClientError

    class _S3:
        def get_object(self, **kw):
            raise ClientError({"Error": {"Code": "404"}}, "GetObject")

    class _B(_StubBackend):
        s3 = _S3()

    age, body = TICK._read_marker(_B(), "k")
    assert age is None and body is None


def test_read_marker_returns_age_and_body():
    class _Body:
        @staticmethod
        def read():
            return json.dumps({"status": "ok", "verdict": "ok"}).encode()

    class _S3:
        def get_object(self, **kw):
            return {"LastModified": dt.datetime.now(dt.timezone.utc)
                    - dt.timedelta(hours=3), "Body": _Body()}

    class _B(_StubBackend):
        s3 = _S3()

    age, body = TICK._read_marker(_B(), "k")
    assert 3 * 3600 - 60 < age < 3 * 3600 + 60
    assert body["status"] == "ok"
