"""Backtest for the detect_overcap ratchet and its watchdog call site ().

guard-5528: an alarm's read-back proves it EXISTS; only a backtest proves it can
FIRE. So these tests drive the detector through a full lifecycle rather than
asserting on one reading -- fire, go quiet, re-fire on regression, CLEAR, and
fire again afterwards. The clear-and-fire-again leg is the one guard-6508 asks
for: a detector wired onto a clock without a ratchet would name the four
permanently-over-cap machine-local stores on every run forever, and an alarm
that cannot be driven quiet is one readers learn to skip.

The fake sweep is deliberate. detect_overcap's own `sweep(apply=False)` reads the
live store registry, whose ratios are a property of THIS box and move under the
test; pinning them here is what makes the ratchet's state machine observable at
all. `_machine_local` is stubbed for the same reason -- it routes through the
own-cloud sync-candidacy SSOT, which is environment-dependent and says nothing
about ratchet behaviour.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # core/scripts
import jsonl_hygiene as jh  # noqa: E402

STORE = "/fixture/world/synced-store.jsonl"
OTHER = "/fixture/meta/known-debt.jsonl"


def _report(path, ratio, action="would-cap"):
    """One sweep report row at a chosen over-cap multiple.

    `kept` is the store's line bound and `total` the live record count, so
    total/kept IS the ratio _overcap_ratio computes. by='lines' is required --
    an age-bounded store has no bound to be a multiple of and is skipped.
    """
    kept = 100
    return {
        "path": path,
        "by": "lines",
        "kept": kept,
        "total": int(round(kept * ratio)),
        "mode": "cap",
        "action": action,
        "owner_goal": "g-fixture",
    }


class _Harness:
    """Drives detect_overcap over a tmp log with pinned ratios."""

    def __init__(self, monkeypatch, tmp_path):
        self.log = tmp_path / "store-hygiene-overcap-log.jsonl"
        self.reports = []
        monkeypatch.setattr(jh, "_overcap_log_path", lambda: self.log)
        monkeypatch.setattr(jh, "sweep",
                            lambda apply=False: {"swept": len(self.reports),
                                                 "reports": list(self.reports)})
        monkeypatch.setattr(jh, "_machine_local", lambda path: (True, None))

    def run(self, ratios, record=True):
        self.reports = [_report(p, r) for p, r in ratios.items()]
        return jh.detect_overcap(record=record)

    def records(self):
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in
                self.log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_first_run_cannot_fire_and_second_run_surfaces(monkeypatch, tmp_path):
    """OUTCOME 2 positive control: a store driven over cap surfaces within two runs.

    Two, not one, is by design: a single over-cap reading is the normal state of
    a store between sweeps, so the consecutive-run rule holds the first one.
    """
    h = _Harness(monkeypatch, tmp_path)

    first = h.run({STORE: 2.0})
    assert first["first_run"] is True
    assert first["fired"] is False, "a first recorded run must never fire"
    assert first["surfaced"] == []

    second = h.run({STORE: 2.0})
    assert second["first_run"] is False
    assert second["repeat_offenders"] == [STORE]
    assert second["newly_over"] == [STORE]
    assert second["surfaced"] == [STORE]
    assert second["fired"] is True, "the wiring must be able to FIRE"


def test_known_debt_surfaces_once_then_goes_quiet(monkeypatch, tmp_path):
    """OUTCOME 3, half one: the ratchet does not re-fire every run."""
    h = _Harness(monkeypatch, tmp_path)
    h.run({OTHER: 8.0})
    surfaced = h.run({OTHER: 8.0})
    assert surfaced["fired"] is True

    for _ in range(3):
        quiet = h.run({OTHER: 8.0})
        assert quiet["fired"] is False, "known debt must not re-fire every run"
        assert quiet["ratcheted_quiet"] == [OTHER]
        # It is still over cap and still a repeat offender -- carried, not hidden.
        assert quiet["repeat_offenders"] == [OTHER]


def test_regression_past_the_ratchet_still_fires(monkeypatch, tmp_path):
    """OUTCOME 3, half two: a store that gets materially worse breaks the quiet."""
    h = _Harness(monkeypatch, tmp_path)
    h.run({OTHER: 8.0})
    h.run({OTHER: 8.0})                      # surfaces, ratchet mark = 8.0
    assert h.run({OTHER: 8.0})["fired"] is False

    # Below the factor: still quiet. 8.0 * 1.5 = 12.0, so 11.9 must NOT fire.
    below = h.run({OTHER: 11.9})
    assert below["fired"] is False, "drift under the regress factor stays quiet"

    at_or_past = h.run({OTHER: 12.0})
    assert at_or_past["regressed"] == [OTHER]
    assert at_or_past["fired"] is True
    # The mark advances to the new level, so the next run is quiet again rather
    # than re-firing on the same regression.
    assert h.run({OTHER: 12.0})["fired"] is False


def test_alarm_clears_and_can_fire_again(monkeypatch, tmp_path):
    """OUTCOME 3, the guard-6508 leg: the alarm can be driven quiet AND back.

    A detector that can only ever accumulate offenders is indistinguishable from
    one that is stuck. Dropping the mark when a store falls back under threshold
    is what makes a later recurrence visible instead of permanently ratcheted
    away.
    """
    h = _Harness(monkeypatch, tmp_path)
    h.run({STORE: 2.0})
    assert h.run({STORE: 2.0})["fired"] is True          # surfaced, mark = 2.0

    cleared = h.run({STORE: 1.0})                        # back under threshold
    assert cleared["ratchet_cleared"] == [STORE]
    assert cleared["over_now"] == {}
    assert cleared["fired"] is False

    # The mark is gone, so the recurrence is NEW again -- one run to re-establish
    # the consecutive-run predecessor, the next to surface.
    assert h.run({STORE: 2.0})["fired"] is False
    again = h.run({STORE: 2.0})
    assert again["newly_over"] == [STORE], "a cleared store must be able to re-fire"
    assert again["fired"] is True


def test_transient_single_reading_never_surfaces(monkeypatch, tmp_path):
    """The consecutive-run rule survives the ratchet: one bump is not an alarm."""
    h = _Harness(monkeypatch, tmp_path)
    h.run({})
    spike = h.run({STORE: 3.0})              # over cap, but no predecessor
    assert spike["fired"] is False
    back = h.run({})                          # gone before the next reading
    assert back["fired"] is False
    assert back["surfaced"] == []


def test_ratchet_is_carried_forward_on_every_recorded_run(monkeypatch, tmp_path):
    """The mark must survive the log's self-truncation.

    The log keeps only the last OVERCAP_LOG_KEEP runs, so a ratchet written once
    and never rewritten would be discarded by ordinary truncation and every
    carried store would surface again. Carrying it forward on every record is
    what makes the state durable in a deliberately bounded file.
    """
    h = _Harness(monkeypatch, tmp_path)
    h.run({OTHER: 8.0})
    h.run({OTHER: 8.0})                       # surfaces, mark written
    for _ in range(jh.OVERCAP_LOG_KEEP + 5):
        h.run({OTHER: 8.0})

    recs = h.records()
    assert len(recs) <= jh.OVERCAP_LOG_KEEP, "the state log must stay bounded"
    assert recs[-1]["ratchet"].get(OTHER) == 8.0
    assert h.run({OTHER: 8.0})["fired"] is False, "mark survived truncation"


def test_watchdog_probe_is_registered_and_reducer_only():
    """OUTCOME 1: the detector has a production call site on a loop clock.

    The probe rides agent-watchdog's --tick, which iteration-close.sh fires at
    every productivity-check -- a clock that does not depend on the store-hygiene
    sweep (g-115-1651) running. Asserting registration rather than mocking the
    tick is deliberate: the defect this goal fixes was a function with no caller,
    so the thing worth pinning is that a caller EXISTS in the registry.
    """
    wd_src = (Path(__file__).resolve().parents[1] / "agent-watchdog.py").read_text(
        encoding="utf-8")
    assert "class StoreOvercapProbe(Probe):" in wd_src
    assert "StoreOvercapProbe(ctx)," in wd_src, "probe must be in build_probes"
    # Reducer-only: one writer per box for the per-box consecutive-run log.
    assert '"store-overcap"' not in wd_src.split("WORKER_SAFE_PROBES = frozenset({")[1].split("})")[0]
