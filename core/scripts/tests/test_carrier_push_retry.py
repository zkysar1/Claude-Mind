"""Tests for the G4 push-retry lane (, carrier_push_retry).

Every test here pins a clause that the pre-apply consult put into the design,
so a future simplification that drops one fails loudly rather than silently
re-opening the defect:
  guard-3849 -- the destination is read and diffed BEFORE the overwriting push
  guard-2104 -- the breadcrumb store merges by sid, never overwrites wholesale
  guard-586  -- the breadcrumb has an explicit expiry that actually fires
  guard-6558 -- `ts` is never restamped by the retry
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import carrier_push_retry as cpr  # noqa: E402

TS = "2026-09-16T01:02:03"
SID = "aaaabbbb-1111-2222-3333-444455556666"


class FakeBackend:
    """Minimal storage backend double: an in-memory authoritative store."""

    def __init__(self, remote=None, write_error=None, read_error=None):
        self.remote = remote
        self.write_error = write_error
        self.read_error = read_error
        self.writes = []
        self.force_fresh_reads = 0

    def read_bytes(self, path, *, force_fresh=False):
        if force_fresh:
            self.force_fresh_reads += 1
        if self.read_error:
            raise self.read_error
        if self.remote is None:
            raise FileNotFoundError(str(path))
        return json.dumps(self.remote).encode("utf-8")

    def write_bytes(self, path, content):
        if self.write_error:
            raise self.write_error
        self.writes.append((Path(path), content))
        return True


@pytest.fixture()
def backend(monkeypatch):
    import storage_backend

    holder = {}

    def install(be):
        holder["be"] = be
        monkeypatch.setattr(storage_backend, "get_backend", lambda: be)
        return be

    return install


def _carrier(state_dir: Path, *, ts=TS, body_state="closed-stale"):
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / f"body-heartbeat-{SID}.json"
    p.write_text(json.dumps({"ts": ts, "body_state": body_state, "sid": SID}),
                 encoding="utf-8")
    return p


def _entry(ts=TS, body_state="closed-stale", first="2026-09-16T01:02:03"):
    return {"sid": SID, "agent": "alpha", "ts": ts, "body_state": body_state,
            "first_failed_at": first, "last_failed_at": first, "attempts": 1}


# ── guard-2104: merge by identity, never a wholesale overwrite ───────────────

def test_record_merges_by_sid_and_preserves_first_failure(tmp_path):
    assert cpr.record_push_failure("alpha", SID, ts=TS, body_state="closed-stale",
                                   state_dir=tmp_path, now="2026-09-16T01:00:00")
    assert cpr.record_push_failure("alpha", SID, ts=TS, body_state="closed-stale",
                                   state_dir=tmp_path, now="2026-09-16T05:00:00")
    data = cpr.load(cpr.breadcrumb_path("alpha", tmp_path))
    assert data[SID]["first_failed_at"] == "2026-09-16T01:00:00"
    assert data[SID]["last_failed_at"] == "2026-09-16T05:00:00"
    assert data[SID]["attempts"] == 2


def test_recording_a_second_sid_does_not_cancel_the_first(tmp_path):
    other = "cccc0000-1111-2222-3333-444455556666"
    cpr.record_push_failure("alpha", SID, ts=TS, body_state="closed-stale",
                            state_dir=tmp_path)
    cpr.record_push_failure("alpha", other, ts=TS, body_state="closed-stale",
                            state_dir=tmp_path)
    data = cpr.load(cpr.breadcrumb_path("alpha", tmp_path))
    assert set(data) == {SID, other}


def test_record_refuses_without_agent_or_sid(tmp_path):
    assert cpr.record_push_failure("", SID, ts=TS, body_state="x",
                                   state_dir=tmp_path) is False
    assert cpr.record_push_failure("alpha", "", ts=TS, body_state="x",
                                   state_dir=tmp_path) is False


def test_load_fails_open_on_garbage(tmp_path):
    p = cpr.breadcrumb_path("alpha", tmp_path)
    p.write_text("{not json", encoding="utf-8")
    assert cpr.load(p) == {}


# ── guard-586: the expiry exists AND fires ──────────────────────────────────

def test_prune_expires_on_first_failure_not_last(tmp_path):
    now = dt.datetime(2026, 9, 20, 0, 0, 0)
    data = {
        SID: _entry(first="2026-09-01T00:00:00") | {
            "last_failed_at": "2026-09-19T23:00:00"},
        "fresh": _entry(first="2026-09-19T00:00:00"),
    }
    kept, expired = cpr.prune(data, ttl_days=7.0, now=now)
    assert expired == [SID], "a row that keeps failing must still age out"
    assert set(kept) == {"fresh"}


def test_prune_keeps_unparseable_first_failed_at(tmp_path):
    data = {SID: _entry(first="not-a-date")}
    kept, expired = cpr.prune(data, ttl_days=1.0, now=dt.datetime(2026, 9, 20))
    assert expired == [] and set(kept) == {SID}


# ── guard-3849: read the destination BEFORE the overwriting transport ───────

def test_destination_newer_refuses_and_retires(tmp_path, backend):
    be = backend(FakeBackend(remote={"ts": "2026-09-16T09:99:99",
                                     "body_state": "active"}))
    _carrier(tmp_path)
    res = cpr.retry_one("alpha", SID, _entry(), tmp_path, apply=True)
    assert res["verdict"] == "destination-newer"
    assert res["retire"] is True
    assert be.writes == [], "a newer destination must never be overwritten"
    assert be.force_fresh_reads == 1, "the destination read must be force_fresh"


def test_already_delivered_is_a_noop(tmp_path, backend):
    be = backend(FakeBackend(remote={"ts": TS, "body_state": "closed-stale"}))
    _carrier(tmp_path)
    res = cpr.retry_one("alpha", SID, _entry(), tmp_path, apply=True)
    assert res["verdict"] == "already-delivered" and res["retire"] is True
    assert be.writes == []


def test_absent_destination_is_not_newer(tmp_path, backend):
    be = backend(FakeBackend(remote=None))
    _carrier(tmp_path)
    res = cpr.retry_one("alpha", SID, _entry(), tmp_path, apply=True)
    assert res["verdict"] == "delivered" and res["retire"] is True
    assert len(be.writes) == 1


# ── the retirement / keep partition ─────────────────────────────────────────

def test_carrier_gone_retires(tmp_path, backend):
    backend(FakeBackend(remote=None))
    res = cpr.retry_one("alpha", SID, _entry(), tmp_path, apply=True)
    assert res["verdict"] == "carrier-gone" and res["retire"] is True


def test_local_moved_retires_without_pushing(tmp_path, backend):
    be = backend(FakeBackend(remote=None))
    _carrier(tmp_path, ts="2026-09-16T22:22:22")
    res = cpr.retry_one("alpha", SID, _entry(ts=TS), tmp_path, apply=True)
    assert res["verdict"] == "local-moved" and res["retire"] is True
    assert be.writes == []


def test_still_refused_keeps_the_breadcrumb(tmp_path, backend):
    backend(FakeBackend(remote=None, write_error=RuntimeError("no_claim")))
    _carrier(tmp_path)
    res = cpr.retry_one("alpha", SID, _entry(), tmp_path, apply=True)
    assert res["verdict"] == "still-refused" and res["retire"] is False
    assert "no_claim" in res["push_error"]


def test_dry_run_never_writes(tmp_path, backend):
    be = backend(FakeBackend(remote=None))
    _carrier(tmp_path)
    res = cpr.retry_one("alpha", SID, _entry(), tmp_path, apply=False)
    assert res["verdict"] == "would-deliver" and res["retire"] is False
    assert be.writes == []


# ── guard-6558: ts is never restamped ───────────────────────────────────────

def test_delivered_payload_is_the_bytes_on_disk_ts_untouched(tmp_path, backend):
    be = backend(FakeBackend(remote=None))
    carrier = _carrier(tmp_path)
    before = carrier.read_bytes()
    cpr.retry_one("alpha", SID, _entry(), tmp_path, apply=True)
    assert be.writes[0][1] == before
    assert json.loads(carrier.read_text(encoding="utf-8"))["ts"] == TS


# ── retry_pending: always a report, and it clears what it delivered ─────────

def test_retry_pending_reports_zero_rather_than_silence(tmp_path):
    rep = cpr.retry_pending("alpha", apply=True, state_dir=tmp_path)
    assert rep["pending"] == 0 and rep["results"] == [] and rep["error"] is None


def test_retry_pending_refuses_without_a_bound_agent(tmp_path):
    rep = cpr.retry_pending("", apply=True, state_dir=tmp_path)
    assert rep["error"] and rep["pending"] == 0


def test_retry_pending_clears_the_breadcrumb_on_delivery(tmp_path, backend):
    backend(FakeBackend(remote=None))
    _carrier(tmp_path)
    cpr.record_push_failure("alpha", SID, ts=TS, body_state="closed-stale",
                            state_dir=tmp_path)
    rep = cpr.retry_pending("alpha", apply=True, state_dir=tmp_path)
    assert rep["pending"] == 1 and rep["retired"] == 1 and rep["kept"] == 0
    assert rep["results"][0]["verdict"] == "delivered"
    assert not cpr.breadcrumb_path("alpha", tmp_path).exists()


def test_retry_pending_keeps_a_still_refused_row(tmp_path, backend):
    backend(FakeBackend(remote=None, write_error=RuntimeError("no_claim")))
    _carrier(tmp_path)
    cpr.record_push_failure("alpha", SID, ts=TS, body_state="closed-stale",
                            state_dir=tmp_path)
    rep = cpr.retry_pending("alpha", apply=True, state_dir=tmp_path)
    assert rep["retired"] == 0 and rep["kept"] == 1
    data = cpr.load(cpr.breadcrumb_path("alpha", tmp_path))
    assert "no_claim" in data[SID]["last_error"]


def test_retry_pending_dry_run_does_not_retire(tmp_path, backend):
    backend(FakeBackend(remote=None))
    _carrier(tmp_path)
    cpr.record_push_failure("alpha", SID, ts=TS, body_state="closed-stale",
                            state_dir=tmp_path)
    rep = cpr.retry_pending("alpha", apply=False, state_dir=tmp_path)
    assert rep["retired"] == 0
    assert cpr.breadcrumb_path("alpha", tmp_path).exists()


# ── the wire-in is REAL, not merely present (guard-1943) ────────────────────

def test_orphan_carrier_repair_calls_the_retry_lane():
    src = (SCRIPTS / "orphan_carrier_repair.py").read_text(encoding="utf-8")
    assert "cpr.retry_pending(agent, apply=args.apply)" in src
    assert "cpr.record_push_failure(" in src
    assert "cpr.REFUSAL_VERDICT" in src


def test_reconcile_orphan_carrier_records_the_breadcrumb():
    src = (SCRIPTS / "body-manifest.py").read_text(encoding="utf-8")
    assert "from carrier_push_retry import record_push_failure" in src
    idx = src.index("def _reconcile_orphan_carrier")
    end = src.index("def _with_repair_verdict")
    body = src[idx:end]
    assert "record_push_failure(" in body
    assert 'return "repaired-push-failed"' in body
