"""evolution-stub-expiry sweep — predicate correctness + idempotency.

Pins finding F2 (2026-05-15): stale awaiting_completion stubs transition to
the schema-defined `expired` status (NOT fabricated [AUTO-FILLED]). Only
stubs older than the threshold expire; recent stubs, final/expired records,
and records with an unparseable timestamp are left untouched.

Run: py -3 core/scripts/tests/test_evolution_stub_expiry.py
"""
import importlib.util
import json
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "evo_stub_expiry", SCRIPT_DIR / "evolution-stub-expiry.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write(p, rows):
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                 encoding="utf-8")


def _rows(p):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def test_old_awaiting_expires_recent_and_terminal_untouched():
    m = load_mod()
    now = datetime(2026, 5, 15, 18, 0, 0)
    old = (now - timedelta(hours=48)).isoformat(timespec="seconds")
    recent = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "skill-evolution.jsonl"
        _write(p, [
            {"revision_id": "skill-a", "status": "awaiting_completion", "ts": old},
            {"revision_id": "skill-b", "status": "awaiting_completion", "ts": recent},
            {"revision_id": "skill-c", "status": "final", "ts": old},
            {"revision_id": "skill-d", "status": "expired", "ts": old},
        ])
        res = m.sweep_stream(p, 24, now, dry_run=False)
        assert res["expired"] == 1, res
        by = {r["revision_id"]: r for r in _rows(p)}
        assert by["skill-a"]["status"] == "expired", by["skill-a"]
        assert by["skill-a"]["expired_by"] == "evolution-stub-expiry"
        assert "expired_at" in by["skill-a"] and "expiry_reason" in by["skill-a"]
        assert by["skill-b"]["status"] == "awaiting_completion", "recent untouched"
        assert by["skill-c"]["status"] == "final", "final untouched"
        assert by["skill-d"]["status"] == "expired", "already-expired untouched"


def test_idempotent_second_pass_no_change():
    m = load_mod()
    now = datetime(2026, 5, 15, 18, 0, 0)
    old = (now - timedelta(hours=48)).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "self-evolution.jsonl"
        _write(p, [{"revision_id": "self-x",
                    "status": "awaiting_completion", "ts": old}])
        first = m.sweep_stream(p, 24, now, dry_run=False)
        second = m.sweep_stream(p, 24, now, dry_run=False)
        assert first["expired"] == 1, first
        assert second["expired"] == 0, "idempotent — nothing left to expire"


def test_unparseable_ts_not_expired():
    m = load_mod()
    now = datetime(2026, 5, 15, 18, 0, 0)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "rule-evolution.jsonl"
        # No ts, revision_id without a parseable timestamp segment.
        _write(p, [{"revision_id": "rule", "status": "awaiting_completion"}])
        res = m.sweep_stream(p, 24, now, dry_run=False)
        assert res["expired"] == 0, "cannot prove staleness -> do not expire"
        assert _rows(p)[0]["status"] == "awaiting_completion"


def test_ts_fallback_from_revision_id():
    m = load_mod()
    now = datetime(2026, 5, 15, 18, 0, 0)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "script-evolution.jsonl"
        # No ts field; timestamp must come from revision_id segment.
        _write(p, [{"revision_id": "script-20260513T120000-zeta-abcd",
                    "status": "awaiting_completion"}])
        res = m.sweep_stream(p, 24, now, dry_run=False)
        assert res["expired"] == 1, "48h-old via revision_id fallback expires"


def test_dry_run_does_not_write():
    m = load_mod()
    now = datetime(2026, 5, 15, 18, 0, 0)
    old = (now - timedelta(hours=48)).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "skill-evolution.jsonl"
        _write(p, [{"revision_id": "k", "status": "awaiting_completion", "ts": old}])
        res = m.sweep_stream(p, 24, now, dry_run=True)
        assert res["expired"] == 1, "dry-run still reports"
        assert _rows(p)[0]["status"] == "awaiting_completion", "but does NOT write"


def test_tz_aware_ts_normalized_not_typeerror():
    """Regression for the 2026-05-15 fresh-eyes finding: ~75% of historical
    stubs carry a tz offset. fromisoformat parses those tz-AWARE; `now` is
    naive; `aware - naive` is a TypeError that silently kills the sweep.
    _parse_ts must normalize tz-aware -> naive-local so an old tz-aware
    awaiting stub expires cleanly instead of crashing."""
    m = load_mod()
    now = datetime(2026, 5, 15, 18, 0, 0)  # naive, like datetime.now()
    from datetime import timezone
    # 48h before `now` in wall-clock, expressed WITH an offset.
    old_aware = datetime(2026, 5, 13, 18, 0, 0,
                         tzinfo=timezone(timedelta(hours=-4))).isoformat()
    assert old_aware.endswith("-04:00"), old_aware  # genuinely tz-aware
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "self-evolution.jsonl"
        _write(p, [{"revision_id": "tz", "status": "awaiting_completion",
                    "ts": old_aware}])
        res = m.sweep_stream(p, 24, now, dry_run=False)  # must not TypeError
        # >=24h past even under the worst plausible tz skew (<14h << 48h).
        assert res["expired"] == 1, res
        assert _rows(p)[0]["status"] == "expired", _rows(p)[0]


def test_no_op_does_not_acquire_write_lock():
    """Regression for the churn finding: locked_modify_jsonl ALWAYS rewrites
    + snapshots .history + appends changelog (no unchanged-skip). When
    nothing is stale, sweep_stream MUST gate it out (no lock, no rewrite),
    else every maintenance tick churns .history across 5 streams x 3 loops."""
    m = load_mod()
    import _fileops
    now = datetime(2026, 5, 15, 18, 0, 0)
    recent = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    orig = _fileops.locked_modify_jsonl
    calls = []

    def spy(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    _fileops.locked_modify_jsonl = spy
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rule-evolution.jsonl"
            _write(p, [{"revision_id": "fresh",
                        "status": "awaiting_completion", "ts": recent}])
            res = m.sweep_stream(p, 24, now, dry_run=False)
            assert res["expired"] == 0, res
            assert calls == [], "no stale stub -> must NOT take the write lock"
            # And when there IS work, it DOES take the lock (gate not stuck).
            old = (now - timedelta(hours=48)).isoformat(timespec="seconds")
            _write(p, [{"revision_id": "stale",
                        "status": "awaiting_completion", "ts": old}])
            res2 = m.sweep_stream(p, 24, now, dry_run=False)
            assert res2["expired"] == 1, res2
            assert calls == [1], "stale stub -> exactly one locked write"
    finally:
        _fileops.locked_modify_jsonl = orig


def run_all():
    tests = [
        test_old_awaiting_expires_recent_and_terminal_untouched,
        test_idempotent_second_pass_no_change,
        test_unparseable_ts_not_expired,
        test_ts_fallback_from_revision_id,
        test_dry_run_does_not_write,
        test_tz_aware_ts_normalized_not_typeerror,
        test_no_op_does_not_acquire_write_lock,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print()
    if failed:
        print(f"FAILED: {failed}/{len(tests)}")
        return 1
    print(f"OK: {len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
