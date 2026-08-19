#!/usr/bin/env python3
"""Lost-update write guard on monitor-stale-check.py ().

WHAT THIS PINS. monitor-stale-check was the FOURTH scan-then-write sweep over
shared goal records and the one left unguarded when its three siblings were
fixed under g-115-6332. Its `_apply_completion` wrote `status=completed` and
then REPLACED `outcome_note` with a ~40-char generated string, so a decision
made at scan time could destroy an arbitrarily large worker outcome_note
committed in between.

TWO PROPERTIES MAKE THE TESTS BELOW WORTH THEIR COST, and they are why a
"the guard exists" presence check would be inadequate:

  * The damage is INVISIBLE to the existing audit. This sweep writes COMPLETED,
    while g-115-6332's damage signature keys on `status=skipped AND completed_by
    AND outcome_class`. A goal it damages is left in a fully plausible terminal
    state, so nothing downstream reports it. The tests are the only detector.
  * A refusal and a no-op look identical from outside. Hence
    `test_refusal_emits_a_metrics_row` — without it, a guard that silently
    stopped refusing would be indistinguishable from a fleet that never raced.

SEAM. Every test patches `mod._reread_goal_authoritative`, the module-global
seam the production code resolves at CALL time (guard-2385). Patching the
shared `_sweep_write_guard` functions instead would still APPLY while the
internal call resolved through the other module's namespace — the stub would
silently stop being consulted and this file would stay green against a disarmed
guard.

STORAGE_BACKEND: conftest.py pins `local` for the pytest session; re-pinned here
explicitly so the file is also safe run main()-style outside pytest, where that
conftest never loads (guard-955 / rb-2983).
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent

os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ["STORAGE_BACKEND"] = "local"

if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from _team_state import (  # noqa: E402
    PROV_AUTHORITATIVE,
    PROV_LOCAL_MIRROR,
    PROV_NONE,
)


def _load_module():
    """Import monitor-stale-check.py (hyphenated filename -> spec loader)."""
    path = CORE_SCRIPTS / "monitor-stale-check.py"
    spec = importlib.util.spec_from_file_location("monitor_stale_check_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scan_goal(goal_id="g-999-01"):
    """The SCAN-time record — deliberately minimal, mirroring what `_load_goals`
    hands `_apply_completion` in production."""
    return {"id": goal_id, "_source": "world", "_asp_id": "asp-999"}


class _PyRecorder:
    """Stand-in for `_py`, recording argv instead of spawning aspirations.py."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, input_text=None):
        self.calls.append(list(args))
        return 0, "", ""

    def field_write(self, field):
        """The argv that wrote `field`, or None."""
        for c in self.calls:
            if "update-goal" in c and field in c:
                return c
        return None


def _apply(mod, fresh, prov, *, metrics_path=None, goal=None):
    """Run `_apply_completion` against a stubbed authoritative re-read."""
    rec = _PyRecorder()
    mod._py = rec
    mod._reread_goal_authoritative = lambda source, goal_id: (fresh, prov)
    ok, detail = mod._apply_completion(
        goal or _scan_goal(), "proc-1800000000", metrics_path=metrics_path
    )
    return ok, detail, rec


# ---------------------------------------------------------------------------
# Outcome 1 — refuses on terminal status / provenance / absence / mirror read
# ---------------------------------------------------------------------------

def test_refuses_when_status_went_terminal_between_scan_and_apply():
    """The measured  race, on this sweep: another box completed the
    goal after the scan listed it."""
    mod = _load_module()
    fresh = {"id": "g-999-01", "status": "completed",
             "outcome_note": "REAL WORKER RECORD " * 300}
    ok, detail, rec = _apply(mod, fresh, PROV_AUTHORITATIVE)

    assert ok is False, "guard allowed a write onto an already-completed goal"
    assert "refused: " in detail
    assert "status changed to" in detail
    assert rec.calls == [], (
        f"guard refused but STILL wrote to the store: {rec.calls!r} — a refusal "
        f"that mutates is not a refusal"
    )


def test_refuses_on_completion_provenance_despite_open_status():
    """`status` alone is not sufficient: a close in flight leaves provenance
    fields while status can still read re-openable."""
    mod = _load_module()
    fresh = {"id": "g-999-01", "status": "in-progress",
             "completed_by": "bravo", "outcome_class": "deep"}
    ok, detail, rec = _apply(mod, fresh, PROV_AUTHORITATIVE)

    assert ok is False
    assert "completion provenance present" in detail
    assert rec.calls == []


def test_refuses_when_goal_absent_from_store_of_record():
    mod = _load_module()
    ok, detail, rec = _apply(mod, None, PROV_NONE)

    assert ok is False
    assert "not found in the store of record" in detail
    assert rec.calls == []


def test_refuses_on_mirror_only_read():
    """FAIL-CLOSED: an unverifiable read is not permission to overwrite. On
    own-cloud the local file is a read-through cache, so a mirror read returns
    the same stale bytes the scan saw — it is not a narrower window."""
    mod = _load_module()
    fresh = {"id": "g-999-01", "status": "pending"}
    ok, detail, rec = _apply(mod, fresh, PROV_LOCAL_MIRROR)

    assert ok is False
    assert "store of record unreachable" in detail
    assert rec.calls == [], "wrote to the store on an unverifiable read"


# ---------------------------------------------------------------------------
# The allow path — the guard must not wedge the sweep shut
# ---------------------------------------------------------------------------

def test_allows_a_clean_open_record():
    """Positive control. Without this, every assertion above would still pass
    against a guard that refuses unconditionally (guard-2421)."""
    mod = _load_module()
    fresh = {"id": "g-999-01", "status": "pending"}
    ok, detail, rec = _apply(mod, fresh, PROV_AUTHORITATIVE)

    assert ok is True, f"guard refused a clean open record: {detail!r}"
    assert "superseded-by-newer-run" in detail
    assert rec.field_write("status") is not None, "status was never written"
    assert rec.field_write("outcome_note") is not None, "outcome_note was never written"


# ---------------------------------------------------------------------------
# The destruction this goal was filed for — outcome_note must be PRESERVED
# ---------------------------------------------------------------------------

def test_existing_outcome_note_is_preserved_not_replaced():
    """`update-goal outcome_note` REPLACES (guard-1691 / guard-3626). The prior
    note is the only durable account of what a worker shipped."""
    mod = _load_module()
    prior = "WORKER RECORD: shipped X, verified Y, mutation-proved Z." * 40
    fresh = {"id": "g-999-01", "status": "pending", "outcome_note": prior}
    ok, detail, rec = _apply(mod, fresh, PROV_AUTHORITATIVE)

    assert ok is True
    argv = rec.field_write("outcome_note")
    assert argv is not None, "outcome_note was never written"
    written = argv[-1]

    # The sweep's own line stays at the HEAD (an existing regression pin greps
    # for this token, and a reader should see the disposition first).
    assert written.startswith("superseded-by-newer-run ("), (
        f"sweep line is no longer at the head: {written[:80]!r}"
    )
    # ...and the prior record survives beneath it. Grep for a marker from the
    # OLD content, never only the new one (guard-3020).
    assert prior in written, (
        "the prior outcome_note was DESTROYED — this is the exact defect "
        "g-115-6415 was filed for"
    )
    assert len(written) > len(prior), "composed note is shorter than what it preserved"


def test_compose_note_is_identity_when_there_is_nothing_to_preserve():
    """Guards against over-correction: a goal with no prior note must not gain
    an empty '[preserved ...]' header."""
    mod = _load_module()
    reason = "superseded-by-newer-run (proc-1800000000)"
    for empty in (None, "", "   "):
        assert mod._compose_note(reason, empty) == reason, (
            f"empty prior note {empty!r} produced a decorated note"
        )


# ---------------------------------------------------------------------------
# Outcome 2 — every refusal emits a metrics row
# ---------------------------------------------------------------------------

def test_refusal_emits_a_metrics_row():
    """A silent refusal is indistinguishable from never having raced, which
    makes the guard's own effectiveness unmeasurable."""
    mod = _load_module()
    with tempfile.TemporaryDirectory(prefix="msc-guard-metrics-") as td:
        mpath = Path(td) / "monitor-stale-check-metrics.jsonl"
        fresh = {"id": "g-999-01", "status": "completed"}
        ok, _detail, _rec = _apply(mod, fresh, PROV_AUTHORITATIVE, metrics_path=mpath)

        assert ok is False
        assert mpath.exists(), "refusal wrote no metrics row"
        rows = [json.loads(ln) for ln in mpath.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
        row = rows[0]
        assert row["type"] == "monitor_stale_refused_stale_candidate"
        assert row["goal_id"] == "g-999-01"
        assert row["source"] == "world"
        assert row["reason"], "refusal row carries no reason"


def test_completion_emits_a_mutation_row_carrying_preserved_length():
    mod = _load_module()
    with tempfile.TemporaryDirectory(prefix="msc-guard-metrics-") as td:
        mpath = Path(td) / "monitor-stale-check-metrics.jsonl"
        prior = "x" * 512
        fresh = {"id": "g-999-01", "status": "pending", "outcome_note": prior}
        ok, _detail, _rec = _apply(mod, fresh, PROV_AUTHORITATIVE, metrics_path=mpath)

        assert ok is True
        rows = [json.loads(ln) for ln in mpath.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(rows) == 1
        assert rows[0]["type"] == "monitor_stale_completed"
        assert rows[0]["preserved_prior_note_chars"] == 512


def test_metrics_failure_never_breaks_the_sweep():
    """Fail-open on metrics: a metrics miss must not turn into a refused sweep
    or a crashed precheck (this runs inside precheck Phase 0)."""
    mod = _load_module()
    unwritable = Path("/nonexistent-dir-msc-guard") / "metrics.jsonl"
    fresh = {"id": "g-999-01", "status": "pending"}
    ok, _detail, _rec = _apply(mod, fresh, PROV_AUTHORITATIVE, metrics_path=unwritable)
    assert ok is True, "an unwritable metrics log blocked the sweep"


# ---------------------------------------------------------------------------
# Scan/guard predicate coupling
# ---------------------------------------------------------------------------

def test_scan_filter_and_guard_share_one_predicate():
    """A scan wider than the guard emits candidates the guard refuses one by
    one, which reads as a broken guard rather than as a scan bug."""
    mod = _load_module()
    assert mod.MONITOR_OPEN_STATUSES == ("pending", "in-progress")

    src = (CORE_SCRIPTS / "monitor-stale-check.py").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    normalized = " ".join(body.split())
    assert 'g.get("status") not in MONITOR_OPEN_STATUSES' in normalized, (
        "main()'s candidate filter no longer reads the shared constant — the "
        "scan and the write guard can now drift apart"
    )


# ---------------------------------------------------------------------------
# guard-1231 — the mutation must reach a visibility CONSUMER
# ---------------------------------------------------------------------------

def _load_surface():
    path = CORE_SCRIPTS / "sweep-mutation-surface.py"
    spec = importlib.util.spec_from_file_location("sweep_mutation_surface_g6415", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_monitor_log_is_registered_with_the_visibility_consumer():
    """guard-1231: emitting a metrics record is NOT surfacing. `SWEEP_LOGS` is
    the entire registration surface — a sweep absent from it is silently
    unsurfaced, which is indistinguishable from a sweep that never fired."""
    surf = _load_surface()
    assert "monitor-stale-check-metrics.jsonl" in surf.SWEEP_LOGS, (
        "monitor-stale-check writes status=completed but its metrics log is not "
        "registered with sweep-mutation-surface — the mutation is invisible"
    )


def test_refusal_rows_are_not_surfaced_as_terminal_mutations():
    """A refusal is the OPPOSITE of a mutation: nothing was written to the goal.
    The generic discriminator (has goal_id, type != run_summary) admits refusal
    rows, and `_format_header` hardcodes the render as '<goal>-> terminal', so
    without this exclusion the surface reports a status change that never
    happened."""
    surf = _load_surface()
    import datetime as _dt

    with tempfile.TemporaryDirectory(prefix="msc-surface-") as td:
        d = Path(td)
        now = _dt.datetime.now()
        ts = now.isoformat(timespec="seconds")
        (d / "monitor-stale-check-metrics.jsonl").write_text(
            json.dumps({"type": "monitor_stale_refused_stale_candidate",
                        "goal_id": "g-REFUSED-01", "timestamp": ts}) + "\n"
            + json.dumps({"type": "monitor_stale_completed",
                          "goal_id": "g-MUTATED-01", "timestamp": ts}) + "\n"
            + json.dumps({"type": "run_summary", "goal_id": "g-IGNORED-01",
                          "timestamp": ts}) + "\n",
            encoding="utf-8",
        )
        watermark = now - _dt.timedelta(hours=1)
        found = surf._collect_new_mutations(str(d), watermark)
        ids = {m["goal_id"] for m in found}

        assert "g-MUTATED-01" in ids, (
            "a real completion row was not collected — the exclusion is too wide"
        )
        assert "g-REFUSED-01" not in ids, (
            "a REFUSAL row was collected and would be rendered as "
            "'g-REFUSED-01->terminal' — reporting a mutation that never happened"
        )
        assert "g-IGNORED-01" not in ids, "run_summary leaked into the mutations"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"{len(fns)} passed")
